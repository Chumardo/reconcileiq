"""Group valid keys in DuckDB and reconcile their source rows without fan-out."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Context, Decimal, localcontext
from numbers import Integral

import duckdb
import pandas as pd

from .config import ColumnMapping, ReconciliationConfig
from .models import ReconciliationRecord, ReconciliationResult, SourceRow
from .validation import (
    INVALID_VALUE_FLAGS,
    MAX_FRACTION_DIGITS,
    MAX_INTEGER_DIGITS,
    normalize_key,
    parse_amount,
    parse_currency,
    parse_status,
)


class SchemaError(ValueError):
    """A source's columns or row locators do not satisfy its mapping."""


_COMPARISON_FLAGS = ("currency_mismatch", "amount_mismatch", "status_mismatch")
_MISSING_FLAGS = ("missing_in_source_a", "missing_in_source_b")
_TOLERATED_MATCH = "matched_within_tolerance"
_TOLERATED_AMOUNT = "amount_within_tolerance"
_CLASSIFICATIONS = (
    "invalid_key", "duplicate_key", *_MISSING_FLAGS, "invalid_value", *_COMPARISON_FLAGS,
    "matched", _TOLERATED_MATCH,
)
_FLAGS = sorted({"invalid_key", "null_key", "duplicate_key", *_MISSING_FLAGS, *_COMPARISON_FLAGS, _TOLERATED_AMOUNT}
                | INVALID_VALUE_FLAGS)
_EXCLUDED_CLASSES = {"invalid_key", "duplicate_key"}


@dataclass(slots=True)
class _PreparedSource:
    rows: list[SourceRow]
    valid_keys: pd.DataFrame
    invalid_rows: list[SourceRow]


def _prepare(
    frame: pd.DataFrame,
    mapping: ColumnMapping,
    key_types: tuple[str, ...],
    row_numbers: Sequence[int] | None,
    source: str,
) -> _PreparedSource:
    if not frame.columns.is_unique:
        raise SchemaError(f"Source {source} has duplicate column names")
    missing = [column for column in mapping.required_columns if column not in frame.columns]
    if missing:
        raise SchemaError(f"Source {source} missing required columns: {', '.join(missing)}")
    locators = range(1, len(frame) + 1) if row_numbers is None else tuple(row_numbers)
    if (len(locators) != len(frame)
            or any(not isinstance(n, Integral) or isinstance(n, bool) or n < 1 for n in locators)
            or len(set(locators)) != len(locators)):
        raise SchemaError(f"Source {source} needs a unique positive row number for each row")

    rows: list[SourceRow] = []
    invalid_rows: list[SourceRow] = []
    keys = []
    columns = (*mapping.keys, mapping.amount, mapping.status, mapping.currency)
    if mapping.row_id is not None:
        columns += (mapping.row_id,)

    values = frame.loc[:, list(columns)].itertuples(index=False, name=None)
    arity = len(key_types)
    for row_index, (locator, value) in enumerate(zip(locators, values, strict=True)):
        raw_key = value[:arity]
        raw_amount, raw_status, raw_currency = value[arity:arity + 3]
        row_id = value[-1] if mapping.row_id is not None else None
        key, issues = normalize_key(raw_key, key_types)
        amount = parse_amount(raw_amount)
        status = parse_status(raw_status)
        currency = parse_currency(raw_currency)
        if amount is None:
            issues.add("invalid_amount")
        if status is None:
            issues.add("invalid_status")
        if currency is None:
            issues.add("invalid_currency")

        row = SourceRow(
            row_number=int(locator), row_id=row_id, amount=amount, status=status, currency=currency,
            raw={"key": raw_key, "amount": raw_amount, "status": raw_status, "currency": raw_currency},
            issues=tuple(sorted(issues)),
        )
        rows.append(row)
        if key is None:
            invalid_rows.append(row)
        else:
            keys.append((row_index, *key))

    key_columns = [f"key_{i}" for i in range(arity)]
    valid_keys = pd.DataFrame(keys, columns=["row_index", *key_columns]).astype(
        {"row_index": "int64", **dict.fromkeys(key_columns, "string")}
    )
    return _PreparedSource(rows, valid_keys, invalid_rows)


def _records_by_key(a: _PreparedSource, b: _PreparedSource, config: ReconciliationConfig):
    # SQL identifiers are generated from key arity. Mapped input names never enter SQL.
    keys = ", ".join(f"key_{i}" for i in range(len(config.key_types)))
    with duckdb.connect(config={"threads": 1}) as connection:
        connection.register("keys_a", a.valid_keys)
        connection.register("keys_b", b.valid_keys)
        groups = connection.execute(f"""
            WITH source_keys AS (
                SELECT *, 'a' AS source FROM keys_a
                UNION ALL
                SELECT *, 'b' AS source FROM keys_b
            )
            SELECT {keys},
                   list(row_index ORDER BY row_index) FILTER (WHERE source = 'a'),
                   list(row_index ORDER BY row_index) FILTER (WHERE source = 'b')
            FROM source_keys
            GROUP BY {keys}
            ORDER BY {keys}
        """).fetchall()

    for *parts, indexes_a, indexes_b in groups:
        key = tuple(parts)
        indexes_a = indexes_a or []
        indexes_b = indexes_b or []
        if len(indexes_a) > 1 or len(indexes_b) > 1:
            for index in indexes_a:
                yield _standalone(key, "duplicate_key", a.rows[index], None)
            for index in indexes_b:
                yield _standalone(key, "duplicate_key", None, b.rows[index])
        elif not indexes_a:
            yield _standalone(key, "missing_in_source_a", None, b.rows[indexes_b[0]])
        elif not indexes_b:
            yield _standalone(key, "missing_in_source_b", a.rows[indexes_a[0]], None)
        else:
            yield _compare(key, a.rows[indexes_a[0]], b.rows[indexes_b[0]], config.amount_tolerance)


def _standalone(
    key: tuple[str, ...] | None, classification: str, a: SourceRow | None, b: SourceRow | None
) -> ReconciliationRecord:
    row = a if a is not None else b
    flags = set(row.issues)
    if classification != "invalid_key":
        flags.add(classification)
    return ReconciliationRecord(key, classification, tuple(sorted(flags)), a, b, None)


def _compare(key: tuple[str, ...], a: SourceRow, b: SourceRow, tolerance: Decimal) -> ReconciliationRecord:
    flags = set(a.issues) | set(b.issues)
    delta = None
    if a.currency is not None and b.currency is not None:
        if a.currency != b.currency:
            flags.add("currency_mismatch")
        elif a.amount is not None and b.amount is not None:
            delta = a.amount - b.amount
            if delta:
                flags.add(_TOLERATED_AMOUNT if delta.copy_abs() <= tolerance else "amount_mismatch")
    if a.status is not None and b.status is not None and a.status != b.status:
        flags.add("status_mismatch")

    if flags & INVALID_VALUE_FLAGS:
        classification = "invalid_value"
    else:
        match = _TOLERATED_MATCH if _TOLERATED_AMOUNT in flags else "matched"
        classification = next((flag for flag in _COMPARISON_FLAGS if flag in flags), match)
    return ReconciliationRecord(key, classification, tuple(sorted(flags)), a, b, delta)


def _summarize(
    records: tuple[ReconciliationRecord, ...], a: _PreparedSource, b: _PreparedSource, tolerance: Decimal
) -> dict:
    counts = Counter(record.classification for record in records)
    flags = Counter(flag for record in records for flag in record.flags)
    matched = counts["matched"] + counts[_TOLERATED_MATCH]
    eligible = len(records) - sum(counts[label] for label in _EXCLUDED_CLASSES)
    currency_totals: dict[str, dict] = {}
    excluded = {"a": 0, "b": 0}
    for side, source in (("a", a), ("b", b)):
        for row in source.rows:
            if row.amount is None or row.currency is None:
                excluded[side] += 1
                continue
            total = currency_totals.setdefault(row.currency, {
                "source_a": Decimal(0), "source_b": Decimal(0), "rows_a": 0, "rows_b": 0,
            })
            total[f"source_{side}"] += row.amount
            total[f"rows_{side}"] += 1
    for total in currency_totals.values():
        total["delta"] = total["source_a"] - total["source_b"]

    return {
        "source_rows_a": len(a.rows),
        "source_rows_b": len(b.rows),
        "result_records": len(records),
        "classification_counts": {label: counts[label] for label in _CLASSIFICATIONS},
        "flag_counts": {flag: flags[flag] for flag in _FLAGS},
        "eligible_records": eligible,
        "matched_records": matched,
        "exact_matched_records": counts["matched"],
        "tolerated_matched_records": counts[_TOLERATED_MATCH],
        "match_rate": matched / eligible if eligible else None,
        "amount_tolerance": tolerance,
        "totals_by_currency": dict(sorted(currency_totals.items())),
        "amounts_excluded_a": excluded["a"],
        "amounts_excluded_b": excluded["b"],
    }


def reconcile(
    source_a: pd.DataFrame,
    source_b: pd.DataFrame,
    config: ReconciliationConfig,
    *,
    row_numbers_a: Sequence[int] | None = None,
    row_numbers_b: Sequence[int] | None = None,
) -> ReconciliationResult:
    """Reconcile two dataframes while retaining each original row exactly once."""
    a = _prepare(source_a, config.source_a, config.key_types, row_numbers_a, "A")
    b = _prepare(source_b, config.source_b, config.key_types, row_numbers_b, "B")

    # Allow enough digits for the widest accepted value plus a carry from every row.
    precision = MAX_INTEGER_DIGITS + MAX_FRACTION_DIGITS + len(str(len(a.rows) + len(b.rows)))
    with localcontext(Context(prec=precision)):
        records = tuple(_records_by_key(a, b, config)) + tuple(
            _standalone(None, "invalid_key", row, None) for row in a.invalid_rows
        ) + tuple(_standalone(None, "invalid_key", None, row) for row in b.invalid_rows)
        summary = _summarize(records, a, b, config.amount_tolerance)
    return ReconciliationResult(records, summary)
