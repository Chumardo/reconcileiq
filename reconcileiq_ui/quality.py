"""Source-specific quality indicators and reconciliation exclusions."""

import altair as alt
import pandas as pd
import streamlit as st

from reconcileiq import ReconciliationResult

from .analysis import quality_by_source
from .dashboard import INK, NAVY, TEAL, chart_style


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


def _issue_chart(quality: dict) -> None:
    rows = pd.DataFrame(
        [
            {"issue": label, "source": name, "rows": quality[source][metric]}
            for metric, label in _ISSUES
            for source, name in _SOURCES
        ]
    )
    source_order = [name for _, name in _SOURCES]
    chart = alt.Chart(rows).encode(
        y=alt.Y("issue:N", sort=[label for _, label in _ISSUES], axis=alt.Axis(ticks=False), title=None),
        yOffset=alt.YOffset("source:N", sort=source_order),
        x=alt.X(
            "rows:Q",
            scale=alt.Scale(domain=[0, max(1, rows["rows"].max() * 1.2)], nice=False),
            axis=alt.Axis(tickMinStep=1, title="Physical source rows"),
        ),
        tooltip=[
            alt.Tooltip("issue:N", title="Issue"),
            alt.Tooltip("source:N", title="Source"),
            alt.Tooltip("rows:Q", title="Rows", format=","),
        ],
    )
    bars = chart.mark_bar(size=11, cornerRadiusEnd=2).encode(
        color=alt.Color(
            "source:N",
            scale=alt.Scale(domain=source_order, range=[TEAL, NAVY]),
            legend=alt.Legend(orient="top", title=None),
        )
    )
    labels = chart.mark_text(align="left", dx=5, color=INK, fontSize=12).encode(text=alt.Text("rows:Q", format=","))
    st.altair_chart(
        chart_style((bars + labels).properties(height=220, description="Quality issues by physical source.")),
        width="stretch",
        theme=None,
    )


def render_quality(result: ReconciliationResult) -> None:
    quality = quality_by_source(result)
    if not result.records:
        st.info("The two sources contain no transaction rows to assess. The counts below are zero.")

    st.subheader("Issue counts by source")
    st.caption("Counts refer to physical source rows. A row can contribute to several issues.")
    _issue_chart(quality)

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
