"""Source-specific quality indicators and reconciliation exclusions."""

from html import escape

import pandas as pd
import streamlit as st

from reconcileiq import ReconciliationResult

from .analysis import quality_by_source


_SOURCES = [("source_a", "Source A"), ("source_b", "Source B")]
_ISSUES = [
    ("duplicate_key_rows", "Rows with repeated keys"),
    ("null_key", "Null or blank key"),
    ("invalid_key", "Invalid key type"),
    ("invalid_amount", "Invalid amount"),
    ("invalid_status", "Invalid status"),
    ("invalid_currency", "Invalid currency"),
]
_MISSING = [
    ("missing_key", "Key component"),
    ("missing_amount", "Amount"),
    ("missing_status", "Status"),
    ("missing_currency", "Currency"),
    ("rows_missing_required", "Rows missing any required value"),
]
_EXCLUSIONS = [
    ("duplicate_keys", "Distinct repeated keys"),
    ("duplicate_key_rows", "Rows with repeated keys in this source"),
    ("duplicate_quarantined_rows", "Rows quarantined for duplicate keys"),
    ("excluded_rows", "Rows excluded from match rate"),
    ("amounts_excluded", "Rows excluded from currency totals"),
]


def _comparison_table(quality: dict, measures: list[tuple[str, str]]) -> None:
    rows = [
        {"Measure": label, **{name: quality[source][metric] for source, name in _SOURCES}}
        for metric, label in measures
    ]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=37 + len(rows) * 35)


def _issue_matrix(quality: dict) -> str:
    rows = [
        (label, quality["source_a"][metric], quality["source_b"][metric])
        for metric, label in _ISSUES
    ]
    rows.sort(key=lambda row: (-(row[1] + row[2]), row[0].casefold()))

    body = []
    for label, count_a, count_b in rows:
        cells = "".join(
            f"<td>{count:,}</td>" if count else "<td>—</td>"
            for count in (count_a, count_b, count_a + count_b)
        )
        body.append(f'<tr><th scope="row">{escape(label)}</th>{cells}</tr>')

    return f"""
<div class="ri-quality-matrix">
    <table aria-label="Data Quality Issue Matrix">
        <thead>
            <tr><th scope="col">Issue</th><th scope="col">Source A</th><th scope="col">Source B</th><th scope="col">Total</th></tr>
        </thead>
        <tbody>{"".join(body)}</tbody>
    </table>
</div>
"""


def render_quality(result: ReconciliationResult) -> None:
    quality = quality_by_source(result)
    if not result.records:
        st.info("The two sources contain no transaction rows to assess. The counts below are zero.")

    st.subheader("Data Quality Issue Matrix")
    st.caption(
        "A dash means zero. Issue counts can overlap: totals count issue occurrences, "
        "not necessarily distinct affected source rows."
    )
    st.html(_issue_matrix(quality))

    missing_column, exclusions_column = st.columns(2)
    with missing_column:
        st.subheader("Missing required values")
        st.caption("Missing values include nulls, empty text, and text containing only whitespace.")
        _comparison_table(quality, _MISSING)
    with exclusions_column:
        st.subheader("Reconciliation exclusions")
        st.caption("Repeated keys are counted within each source. Quarantine can include a unique counterpart.")
        _comparison_table(quality, _EXCLUSIONS)

    with st.expander("How data quality affects reconciliation"):
        st.markdown(
            """
- **Key issues.** A missing or invalid key component excludes its row from the match-rate denominator.
- **Duplicate keys.** When a key repeats within either source, every row with that key is quarantined. That includes a unique counterpart in the other source.
- **Invalid values.** A valid key that appears at most once per source remains eligible. Invalid required values prevent a match and may prevent an amount or status comparison.
- **Currency totals.** Rows with an invalid amount or currency are excluded from currency totals. Rows with valid amounts and currencies contribute even when their keys are invalid or quarantined.
"""
        )
