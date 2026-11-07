"""Paginated reconciliation results and side-by-side source evidence."""

from decimal import Decimal
from hashlib import sha256

import pandas as pd
import streamlit as st

from reconcileiq import ReconciliationConfig, ReconciliationRecord, ReconciliationResult, SourceRow
from reconcileiq.audit import MATCHED_CLASSES, MISSING_CLASSES, issue_impact, unmatched_amount

from .analysis import (
    CLASSIFICATION_LABELS, FLAG_LABELS,
    duplicate_group, filter_exceptions, key_text, record_currencies,
)


def _money(value: Decimal | None) -> str:
    return "—" if value is None else format(value, "f")


def _display(value: object) -> str:
    return "—" if value is None else str(value)


def _key(record: ReconciliationRecord) -> str:
    if record.key is not None:
        return key_text(record.key)
    row = record.source_a or record.source_b
    return " / ".join(repr(part) for part in row.raw["key"])


def _grid_record(record: ReconciliationRecord) -> dict[str, str]:
    a, b = record.source_a, record.source_b
    return {
        "Key": _key(record),
        "Primary outcome": CLASSIFICATION_LABELS[record.classification],
        "Impact": issue_impact(record),
        "Currency": " / ".join(sorted(record_currencies(record))) or "—",
        "A − B": _money(record.amount_delta),
        "Unmatched Amount": _money(unmatched_amount(record)),
        "Issues": ", ".join(FLAG_LABELS[flag] for flag in record.flags),
        "Source A ID": _display(a.row_id if a else None),
        "Source B ID": _display(b.row_id if b else None),
        "Amount A": _money(a.amount if a else None),
        "Amount B": _money(b.amount if b else None),
    }


def _source_fields(row: SourceRow | None, source_file: str) -> dict[str, str]:
    if row is None:
        return {"Source file / worksheet": source_file}
    return {
        "Source file / worksheet": source_file,
        "Original row ID": _display(row.row_id),
        "File row": str(row.row_number),
        "Original key components": ", ".join(repr(part) for part in row.raw["key"]),
        "Parsed amount": _money(row.amount),
        "Original amount": repr(row.raw["amount"]),
        "Status": _display(row.status),
        "Original status": repr(row.raw["status"]),
        "Currency": _display(row.currency),
        "Original currency": repr(row.raw["currency"]),
        "Source validation issues": ", ".join(FLAG_LABELS[flag] for flag in row.issues) or "None",
    }


def _amount_comparison_note(record: ReconciliationRecord) -> str | None:
    if record.classification in MISSING_CLASSES:
        if unmatched_amount(record) is None:
            return "The counterpart is absent. The available row needs a valid amount and currency to show an unmatched amount."
        return "The counterpart is absent. The unmatched amount is the signed amount from the available source."
    if record.classification == "duplicate_key":
        return "The key repeats within a source. All rows for the key are quarantined before amounts are compared."
    if record.classification == "invalid_key":
        return "An invalid or missing key prevents pairing the source row."
    if record.amount_delta is None:
        if "currency_mismatch" in record.flags:
            return "The source currencies differ. Comparing amounts requires the same currency on both rows."
        if "invalid_currency" in record.flags:
            return "Comparing amounts requires a valid currency on both rows."
        return "Comparing amounts requires a valid amount on both rows."
    if "amount_within_tolerance" in record.flags:
        return "The nonzero absolute amount difference falls within the applied tolerance."
    return None


def _render_duplicate_group(
    records: tuple[ReconciliationRecord, ...], record: ReconciliationRecord, source_a: str, source_b: str,
) -> None:
    group = duplicate_group(records, record.key)
    rows = []
    for group_record in group:
        for side, file_name, row in (
            ("Source A", source_a, group_record.source_a), ("Source B", source_b, group_record.source_b),
        ):
            if row is None:
                continue
            fields = _source_fields(row, file_name)
            rows.append({"Source": side, **fields})
    st.subheader("Duplicate key group", anchor=False)
    a_count = sum(row["Source"] == "Source A" for row in rows)
    st.caption(f"Group rows · Source A {a_count:,} · Source B {len(rows) - a_count:,}. Includes rows outside the current filters and page.")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=min(360, 38 + len(rows) * 35))


def _render_comparison_rules(config: ReconciliationConfig | None, tolerance: Decimal) -> None:
    with st.expander("Applied comparison rules"):
        st.caption(f"Amount tolerance is {_money(tolerance)} per currency. A nonzero difference qualifies when |A − B| is at most the tolerance.")
        st.caption(
            "Amounts require a shared currency and valid decimals. Status comparisons are case-sensitive after trimming whitespace. "
            "Text key parts trim whitespace. Integer key parts normalize signs and leading zeros. Invalid keys and duplicate groups are quarantined."
        )
        if config is not None:
            a, b = config.source_a, config.source_b
            rows = [
                {"Rule": f"Key part {index} ({kind})", "Source A column": a_name, "Source B column": b_name}
                for index, (a_name, b_name, kind) in enumerate(zip(a.keys, b.keys, config.key_types, strict=True), start=1)
            ]
            rows.extend(
                {"Rule": label, "Source A column": _display(getattr(a, field)), "Source B column": _display(getattr(b, field))}
                for field, label in (("amount", "Amount"), ("currency", "Currency"), ("status", "Status"), ("row_id", "Original row ID"))
            )
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def _render_evidence(
    record: ReconciliationRecord, result: ReconciliationResult, source_a: str, source_b: str,
    config: ReconciliationConfig | None,
) -> None:
    st.subheader("Record evidence", anchor=False)
    st.text(_key(record))
    impact = issue_impact(record)
    if impact != "None":
        color = {"High": "red", "Review": "orange", "Within tolerance": "blue"}[impact]
        st.badge(impact, color=color)
    st.caption(f"{CLASSIFICATION_LABELS[record.classification]} · A − B: {_money(record.amount_delta)}")
    unmatched = unmatched_amount(record)
    if unmatched is not None:
        present = record.source_a or record.source_b
        st.caption(f"Unmatched amount · {_money(unmatched)} {present.currency}")
    note = _amount_comparison_note(record)
    if note:
        st.caption(note)
    if record.flags:
        st.caption("Issue flags · " + ", ".join(FLAG_LABELS[flag] for flag in record.flags))
    if record.classification == "duplicate_key":
        _render_duplicate_group(result.records, record, source_a, source_b)

    a, b = _source_fields(record.source_a, source_a), _source_fields(record.source_b, source_b)
    evidence = pd.DataFrame([
        {"Field": field, "Source A": a.get(field, "—"), "Source B": b.get(field, "—")}
        for field in dict.fromkeys((*a, *b))
    ])
    st.dataframe(evidence, hide_index=True, width="stretch", height=423, column_config={"Field": "Source field"})
    tolerance = config.amount_tolerance if config is not None else result.summary.get("amount_tolerance", Decimal(0))
    _render_comparison_rules(config, tolerance)


def render_explorer(
    result: ReconciliationResult, run_serial: int, *, source_a: str = "Source A", source_b: str = "Source B",
    config: ReconciliationConfig | None = None,
) -> None:
    records = result.records
    if not records:
        st.info("This audit has no reconciliation records. Both sources were empty.")
        return

    matched_count = result.summary["matched_records"]
    tolerated_count = result.summary["classification_counts"].get("matched_within_tolerance", 0)
    scope_options = ("Exceptions", "All records", "Within tolerance") if tolerated_count else ("Exceptions", "All records")
    scope_column, search_column = st.columns((1.25, 2), vertical_alignment="center")
    scope = scope_column.radio(
        "Record scope", scope_options, horizontal=True,
        key=f"record_scope_{run_serial}",
        help="Exceptions include unresolved issues and quarantined rows. All records also includes exact and within-tolerance matches.",
    )
    if scope == "Exceptions":
        population = tuple(record for record in records if record.classification not in MATCHED_CLASSES)
    elif scope == "Within tolerance":
        population = tuple(record for record in records if record.classification == "matched_within_tolerance")
    else:
        population = records
    primary_in_scope = {record.classification for record in population}
    primary_options = [key for key in CLASSIFICATION_LABELS if key in primary_in_scope]
    issue_options = [key for key in FLAG_LABELS if result.summary["flag_counts"].get(key)]
    currency_options = sorted({code for record in records for code in (record_currencies(record) or {""})})
    search = search_column.text_input(
        "Search transaction keys", placeholder="Search a key or key component",
        key=f"record_query_{run_serial}",
        help="Case-insensitive search across the normalized key and original key components.",
    )
    left, center, right = st.columns((1.1, 1.1, 0.8))
    classifications = left.multiselect(
        "Primary classification", primary_options, format_func=CLASSIFICATION_LABELS.__getitem__,
        key=f"record_primary_{run_serial}_{scope}", placeholder="Any classification",
    )
    flags = center.multiselect(
        "Issue flags", issue_options, format_func=FLAG_LABELS.__getitem__,
        key=f"record_flags_{run_serial}", placeholder="Any issue",
        help="A record may carry several flags. A record is included if it has any selected flag.",
    )
    currencies = right.multiselect(
        "Currency", currency_options, format_func=lambda code: code or "No valid currency",
        key=f"record_currency_{run_serial}", placeholder="Any currency",
        help="A record is included if either source has a selected currency.",
    )
    filtered = filter_exceptions(
        population, classifications=classifications, flags=flags, currencies=currencies, query=search,
    )
    if scope == "Exceptions" and matched_count:
        st.caption(f"{matched_count:,} matched records are outside this scope. Choose All records to include them.")
    with st.expander("Impact and amount fields"):
        st.write(
            "High covers missing counterparts, quarantined keys, invalid values, and currency differences. "
            "Review covers amount and status mismatches. Within tolerance identifies an otherwise matching pair with a nonzero accepted difference."
        )
        st.write(
            "Unmatched Amount is the signed amount from the available source when a counterpart is missing. "
            "A dash indicates the amount is unavailable or the field does not apply. A − B requires two valid amounts in the same currency."
        )
    if not filtered:
        st.info(f"No records match these filters. There are {len(population):,} records in the current scope.")
        return

    size_column, page_column, _ = st.columns((1, 1, 3), vertical_alignment="bottom")
    page_size = size_column.selectbox("Rows per page", (25, 50, 100), index=1, key=f"page_size_{run_serial}")
    pages = (len(filtered) + page_size - 1) // page_size
    view_key = sha256(repr((run_serial, scope, classifications, flags, currencies, search, page_size)).encode()).hexdigest()[:16]
    page = page_column.number_input("Page", min_value=1, max_value=pages, value=1, key=f"page_{view_key}") or 1
    offset = (page - 1) * page_size
    visible = filtered[offset:offset + page_size]
    st.caption(
        f"Showing {offset + 1:,}–{offset + len(visible):,} of {len(filtered):,} filtered records · "
        f"{len(population):,} in scope · Page {page:,} of {pages:,}"
    )
    selection = st.dataframe(
        pd.DataFrame([_grid_record(record) for record in visible]),
        hide_index=True, width="stretch", height=min(360, 38 + 35 * len(visible)),
        on_select="rerun", selection_mode="single-row", key=f"records_{view_key}_{page}",
        column_config={
            "Key": st.column_config.TextColumn(pinned=True),
            "Issues": st.column_config.TextColumn(width="medium"),
            "Unmatched Amount": st.column_config.TextColumn(help="Signed amount on the available source for a missing counterpart."),
        },
    )
    if selection.selection.rows:
        _render_evidence(visible[selection.selection.rows[0]], result, source_a, source_b, config)
    else:
        st.caption("Select a row to inspect its source evidence side by side.")
