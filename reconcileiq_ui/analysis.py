"""Read-only projections of a completed audit for the Streamlit workspace."""

from collections.abc import Iterable, Sequence

import pandas as pd

from reconcileiq import ReconciliationRecord, ReconciliationResult
from reconcileiq.audit import dashboard_metrics as dashboard_metrics
from reconcileiq.audit import quality_by_source as quality_by_source


CLASSIFICATION_LABELS = {
    "matched": "Exact match",
    "matched_within_tolerance": "Matched within tolerance",
    "missing_in_source_a": "Missing in A",
    "missing_in_source_b": "Missing in B",
    "amount_mismatch": "Amount mismatch",
    "status_mismatch": "Status mismatch",
    "currency_mismatch": "Currency mismatch",
    "invalid_value": "Invalid value",
    "duplicate_key": "Duplicate key",
    "invalid_key": "Invalid or null key",
}
FLAG_LABELS = {
    "missing_in_source_a": "Missing in A",
    "missing_in_source_b": "Missing in B",
    "amount_mismatch": "Amount mismatch",
    "amount_within_tolerance": "Amount within tolerance",
    "status_mismatch": "Status mismatch",
    "currency_mismatch": "Currency mismatch",
    "duplicate_key": "Duplicate-key quarantine",
    "null_key": "Null key",
    "invalid_key": "Invalid key type",
    "invalid_amount": "Invalid amount",
    "invalid_status": "Invalid status",
    "invalid_currency": "Invalid currency",
}

def currency_totals(result: ReconciliationResult) -> pd.DataFrame:
    """Keep the engine's Decimal totals and row counts at their currency grain."""
    rows = [
        (currency, total["source_a"], total["source_b"], total["delta"], total["rows_a"], total["rows_b"])
        for currency, total in result.summary["totals_by_currency"].items()
    ]
    return pd.DataFrame(rows, columns=["Currency", "Source A", "Source B", "A − B", "Rows in A", "Rows in B"])


def duplicate_group(
    records: Iterable[ReconciliationRecord], key: tuple[str, ...] | None,
) -> tuple[ReconciliationRecord, ...]:
    """Return every quarantined row for a normalized key, in engine order."""
    if key is None:
        return ()
    return tuple(record for record in records if record.classification == "duplicate_key" and record.key == key)


def record_currencies(record: ReconciliationRecord) -> set[str]:
    """Return the distinct valid currencies on either side of a result."""
    return {row.currency for row in (record.source_a, record.source_b) if row and row.currency is not None}


def key_text(parts: Sequence) -> str:
    return " / ".join(str(part) for part in parts)


def _key_matches(record: ReconciliationRecord, query: str) -> bool:
    keys = [record.key] if record.key is not None else []
    keys.extend(row.raw["key"] for row in (record.source_a, record.source_b) if row is not None)
    return any(query in key_text(key).casefold() for key in keys)


def filter_exceptions(
    records: Iterable[ReconciliationRecord],
    *,
    classifications: Iterable[str] = (),
    flags: Iterable[str] = (),
    currencies: Iterable[str] = (),
    query: str = "",
) -> tuple[ReconciliationRecord, ...]:
    """Filter with OR within a control and AND across controls, preserving order."""
    classification_filter = set(classifications)
    flag_filter = set(flags)
    currency_filter = set(currencies)
    query = query.strip().casefold()
    filtered = []
    for record in records:
        if classification_filter and record.classification not in classification_filter:
            continue
        if flag_filter and flag_filter.isdisjoint(record.flags):
            continue
        if currency_filter and currency_filter.isdisjoint(record_currencies(record) or {""}):
            continue
        if query and not _key_matches(record, query):
            continue
        filtered.append(record)
    return tuple(filtered)
