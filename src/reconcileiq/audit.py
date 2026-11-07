"""Shared, read-only audit measures for the workspace and workbook."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Context, Decimal, localcontext

from pandas import api, isna

from .models import ReconciliationRecord, ReconciliationResult
from .validation import MAX_FRACTION_DIGITS, MAX_INTEGER_DIGITS


MATCHED_CLASSES = frozenset({"matched", "matched_within_tolerance"})
EXCLUDED_CLASSES = frozenset({"duplicate_key", "invalid_key"})
MISSING_CLASSES = frozenset({"missing_in_source_a", "missing_in_source_b"})
_HIGH_IMPACT = MISSING_CLASSES | EXCLUDED_CLASSES | {"invalid_value", "currency_mismatch"}
_VALUE_FIELDS = ("amount", "status", "currency")
_QUALITY_FIELDS = (
    "source_rows", "duplicate_keys", "duplicate_key_rows", "duplicate_quarantined_rows",
    "invalid_key", "null_key", "invalid_amount", "invalid_status", "invalid_currency",
    "missing_key", "missing_amount", "missing_status", "missing_currency",
    "rows_missing_required", "excluded_rows", "amounts_excluded",
)
_ZERO = Decimal(0)


def dashboard_metrics(result: ReconciliationResult) -> dict:
    """Expose the engine's metric grain and derive disjoint outcome groups."""
    summary = result.summary
    metrics = {name: summary[name] for name in (
        "source_rows_a", "source_rows_b", "result_records", "matched_records", "match_rate",
        "eligible_records", "classification_counts", "flag_counts", "exact_matched_records",
        "tolerated_matched_records", "amount_tolerance",
    )}
    metrics["excluded_records"] = summary["result_records"] - summary["eligible_records"]
    metrics["eligible_exceptions"] = summary["eligible_records"] - summary["matched_records"]
    return metrics


def issue_impact(record: ReconciliationRecord) -> str:
    """Describe the review priority of the primary outcome, independent of money size."""
    if record.classification in _HIGH_IMPACT:
        return "High"
    if record.classification in {"amount_mismatch", "status_mismatch"}:
        return "Review"
    if record.classification == "matched_within_tolerance":
        return "Within tolerance"
    return "None"


def unmatched_amount(record: ReconciliationRecord) -> Decimal | None:
    """Return the original signed amount for a missing counterpart, when denominated."""
    if record.classification in MISSING_CLASSES:
        row = record.source_a or record.source_b
        if row is not None and row.currency is not None:
            return row.amount
    return None


def _is_missing(value: object) -> bool:
    if isinstance(value, str):
        return not value.strip()
    return api.types.is_scalar(value) and not isinstance(value, Decimal) and bool(isna(value))


def quality_by_source(result: ReconciliationResult) -> dict[str, dict[str, int]]:
    """Count overlapping issues at the physical source-row grain."""
    sources = ("source_a", "source_b")
    counts = {source: Counter() for source in sources}
    quarantine_keys = {source: Counter() for source in sources}
    for record in result.records:
        for source, row in zip(sources, (record.source_a, record.source_b), strict=True):
            if row is None:
                continue
            count = counts[source]
            count["source_rows"] += 1
            count.update(row.issues)
            count["excluded_rows"] += record.classification in EXCLUDED_CLASSES
            if record.classification == "duplicate_key":
                count["duplicate_quarantined_rows"] += 1
                quarantine_keys[source][record.key] += 1

            missing_fields = [f"missing_{name}" for name in _VALUE_FIELDS if _is_missing(row.raw[name])]
            if any(_is_missing(component) for component in row.raw["key"]):
                missing_fields.append("missing_key")
            count.update(missing_fields)
            count["rows_missing_required"] += bool(missing_fields)

    for source, side in zip(sources, ("a", "b"), strict=True):
        repetitions = [count for count in quarantine_keys[source].values() if count > 1]
        counts[source]["duplicate_keys"] = len(repetitions)
        counts[source]["duplicate_key_rows"] = sum(repetitions)
        counts[source]["amounts_excluded"] = result.summary[f"amounts_excluded_{side}"]
    return {source: {field: counts[source][field] for field in _QUALITY_FIELDS} for source in sources}


@dataclass(frozen=True, slots=True)
class CurrencyAudit:
    currency: str
    source_a: Decimal
    source_b: Decimal
    source_delta: Decimal
    amount_rows_a: int
    amount_rows_b: int
    currency_rows_a: int
    currency_rows_b: int
    compared_a: Decimal
    compared_b: Decimal
    compared_delta: Decimal
    compared_pairs: int
    matched_a: Decimal
    matched_b: Decimal
    matched_delta: Decimal
    matched_pairs: int

    @property
    def coverage(self) -> str:
        if not self.currency_rows_a:
            return "Missing in Source A"
        if not self.currency_rows_b:
            return "Missing in Source B"
        if not self.amount_rows_a and not self.amount_rows_b:
            return "No valid amounts in either source"
        if not self.amount_rows_a:
            return "No valid amounts in Source A"
        if not self.amount_rows_b:
            return "No valid amounts in Source B"
        return "Both sources"


@dataclass(slots=True)
class _PairTotals:
    a: Decimal = _ZERO
    b: Decimal = _ZERO
    pairs: int = 0

    def add(self, a: Decimal, b: Decimal) -> None:
        self.a += a
        self.b += b
        self.pairs += 1

    @property
    def delta(self) -> Decimal:
        return self.a - self.b


def currency_audit(result: ReconciliationResult) -> tuple[CurrencyAudit, ...]:
    """Keep source, compared, and matched totals separate within each currency."""
    source_totals = result.summary["totals_by_currency"]
    valid_currency_a, valid_currency_b = Counter(), Counter()
    compared, matched = defaultdict(_PairTotals), defaultdict(_PairTotals)
    source_count = result.summary["source_rows_a"] + result.summary["source_rows_b"]
    precision = MAX_INTEGER_DIGITS + MAX_FRACTION_DIGITS + len(str(source_count))
    with localcontext(Context(prec=precision)):
        for record in result.records:
            a, b = record.source_a, record.source_b
            if a is not None and a.currency is not None:
                valid_currency_a[a.currency] += 1
            if b is not None and b.currency is not None:
                valid_currency_b[b.currency] += 1
            if record.amount_delta is not None:
                compared[a.currency].add(a.amount, b.amount)
                if record.classification in MATCHED_CLASSES:
                    matched[a.currency].add(a.amount, b.amount)

        rows = []
        for code in sorted(valid_currency_a.keys() | valid_currency_b.keys()):
            source = source_totals.get(code, {})
            compared_total, matched_total = compared[code], matched[code]
            rows.append(CurrencyAudit(
                currency=code,
                source_a=source.get("source_a", _ZERO), source_b=source.get("source_b", _ZERO),
                source_delta=source.get("delta", _ZERO),
                amount_rows_a=source.get("rows_a", 0), amount_rows_b=source.get("rows_b", 0),
                currency_rows_a=valid_currency_a[code], currency_rows_b=valid_currency_b[code],
                compared_a=compared_total.a, compared_b=compared_total.b,
                compared_delta=compared_total.delta, compared_pairs=compared_total.pairs,
                matched_a=matched_total.a, matched_b=matched_total.b,
                matched_delta=matched_total.delta, matched_pairs=matched_total.pairs,
            ))
    return tuple(rows)
