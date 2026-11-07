"""Reconciliation overview and the currency comparison."""

from decimal import Decimal

import altair as alt
import pandas as pd
import streamlit as st

from reconcileiq import ReconciliationResult
from reconcileiq.audit import CurrencyAudit, currency_audit

from .analysis import CLASSIFICATION_LABELS, FLAG_LABELS, dashboard_metrics


TEAL = "#08766D"
INK = "#15262D"
NAVY = "#4D6572"
AMBER = "#AA681D"
_GRID = "#E1E8EB"
_FONT = "Inter, system-ui, sans-serif"
_QUALITY_FLAGS = {"duplicate_key", "invalid_key", "null_key", "invalid_amount", "invalid_status", "invalid_currency"}


def chart_style(chart):
    """Apply a shared visual language to the overview and quality charts."""
    return (
        chart.configure(background="transparent", font=_FONT)
        .configure_view(strokeWidth=0)
        .configure_axis(
            domainColor=_GRID,
            gridColor=_GRID,
            gridOpacity=0.7,
            labelColor=NAVY,
            labelFontSize=12,
            labelPadding=7,
            tickColor=_GRID,
            titleColor=NAVY,
            titleFontSize=11,
            titlePadding=9,
        )
        .configure_legend(labelColor=NAVY, labelFontSize=12, symbolType="square")
    )


def _exception_flags(counts: dict[str, int]) -> None:
    st.subheader("Issue flags")
    st.caption("A record can have multiple flags, so these counts can overlap.")
    rows = pd.DataFrame(
        [
            {
                "issue": FLAG_LABELS[flag],
                "records": count,
                "type": "Source quality" if flag in _QUALITY_FLAGS else "Comparison",
            }
            for flag, count in counts.items()
        ]
    ).sort_values(["records", "issue"], ascending=[False, True])
    chart = alt.Chart(rows).encode(
        y=alt.Y("issue:N", sort=rows["issue"].tolist(), title=None, axis=alt.Axis(ticks=False, labelLimit=175)),
        x=alt.X(
            "records:Q",
            scale=alt.Scale(domain=[0, max(1, rows["records"].max() * 1.25)], nice=False),
            axis=alt.Axis(tickMinStep=1, title="Result records"),
        ),
        tooltip=[alt.Tooltip("issue:N", title="Issue flag"), alt.Tooltip("records:Q", title="Records", format=",")],
    )
    bars = chart.mark_bar(size=13, cornerRadiusEnd=2).encode(
        color=alt.Color(
            "type:N",
            scale=alt.Scale(domain=["Comparison", "Source quality"], range=[TEAL, NAVY]),
            legend=alt.Legend(orient="top", title=None),
        )
    )
    labels = chart.mark_text(align="left", dx=5, color=INK, fontSize=12).encode(text=alt.Text("records:Q", format=","))
    st.altair_chart(
        chart_style((bars + labels).properties(height=len(rows) * 23, description="Result records by issue flag.")),
        width="stretch",
        theme=None,
    )


def _money(value: Decimal, included: bool | int = True) -> str:
    return format(value, ",f") if included else "—"


def _currency_differences(totals: tuple[CurrencyAudit, ...]) -> None:
    st.subheader("Amount differences by currency")
    if not totals:
        st.info("No rows have a valid currency.")
        return

    by_currency = {row.currency: row for row in totals}
    currencies = list(by_currency)
    initial = next((index for index, row in enumerate(totals) if row.compared_pairs), 0)
    selected = by_currency[st.selectbox("Currency in chart", currencies, index=initial)]
    st.caption("Bars show the absolute difference in the selected currency. Tooltips show the signed A minus B difference.")
    comparisons = (
        ("Source totals", selected.source_delta, selected.amount_rows_a + selected.amount_rows_b, NAVY),
        ("Compared pairs", selected.compared_delta, selected.compared_pairs, AMBER),
        ("Reconciled pairs", selected.matched_delta, selected.matched_pairs, TEAL),
    )
    rows = [
        {"Scope": label, "Magnitude": float(delta.copy_abs()), "A − B": _money(delta)}
        for label, delta, count, _ in comparisons if count
    ]
    if not rows:
        st.info(f"No valid amounts were available in {selected.currency}.")
        return

    scopes = [label for label, _, _, _ in comparisons]
    chart = alt.Chart(pd.DataFrame(rows)).encode(
        x=alt.X(
            "Magnitude:Q", scale=alt.Scale(zero=True),
            axis=alt.Axis(title=f"Absolute A − B ({selected.currency})", format="~s", tickCount=4),
        ),
        y=alt.Y("Scope:N", sort=scopes, title=None, axis=alt.Axis(ticks=False)),
        color=alt.Color(
            "Scope:N", legend=None,
            scale=alt.Scale(domain=scopes, range=[color for _, _, _, color in comparisons]),
        ),
        tooltip=[alt.Tooltip("Scope:N"), alt.Tooltip("A − B:N", title=f"Exact A − B ({selected.currency})")],
    )
    bars = (chart.mark_bar(size=12, cornerRadiusEnd=2) + chart.mark_circle(size=38, opacity=1)).properties(
        height=max(72, 32 * len(rows)), description=f"Absolute monetary differences in {selected.currency}.",
    )
    st.altair_chart(chart_style(bars), width="stretch", theme=None)
    if not selected.compared_pairs:
        st.caption("No pairs had valid amounts in the same currency.")
    elif not selected.matched_pairs:
        st.caption("No compared pairs qualified as reconciled.")


def _currency_totals_table(totals: tuple[CurrencyAudit, ...]) -> None:
    if not totals:
        return
    st.subheader("Currency totals and coverage")
    missing = [row.currency for row in totals if not row.currency_rows_a or not row.currency_rows_b]
    if missing:
        st.badge(f"Currency missing from a source · {', '.join(missing[:5])}"
                 + (f" and {len(missing) - 5:,} more" if len(missing) > 5 else ""), color="orange")

    view = st.radio(
        "Totals scope", ("Source totals", "Compared pairs", "Reconciled pairs"), horizontal=True,
        help="Source totals include all valid amounts by currency. Compared pairs share a key and currency. "
             "Reconciled pairs have an exact or within-tolerance match.",
    )
    table = []
    for row in totals:
        cells = {"Currency": row.currency, "Coverage": row.coverage}
        if view == "Source totals":
            cells.update({
                "Source A": _money(row.source_a, row.amount_rows_a),
                "Source B": _money(row.source_b, row.amount_rows_b),
                "A − B": _money(row.source_delta, row.amount_rows_a + row.amount_rows_b),
                "Amount rows in A": row.amount_rows_a, "Amount rows in B": row.amount_rows_b,
            })
        else:
            count, a, b, delta = (
                (row.compared_pairs, row.compared_a, row.compared_b, row.compared_delta)
                if view == "Compared pairs" else (row.matched_pairs, row.matched_a, row.matched_b, row.matched_delta)
            )
            cells.update({"Source A": _money(a, count), "Source B": _money(b, count), "A − B": _money(delta, count), "Pairs": count})
        table.append(cells)
    st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch", height=min(255, 38 + len(table) * 35))
    st.caption(
        "Coverage uses valid currency codes. Source totals include every row with a valid amount and currency. "
        "Compared pairs require valid amounts and a shared currency. "
        "Reconciled pairs count exact and within-tolerance matches. A dash indicates no amount to total in that scope."
    )


def render_dashboard(result: ReconciliationResult) -> None:
    metrics = dashboard_metrics(result)
    matched, eligible = metrics["matched_records"], metrics["eligible_records"]
    rate = metrics["match_rate"]
    counts = metrics["classification_counts"]
    columns = st.columns(4)
    columns[0].metric("Matched", f"{matched:,}")
    columns[0].caption(f"{metrics['exact_matched_records']:,} exact · {metrics['tolerated_matched_records']:,} within tolerance")
    columns[1].metric("Eligible exceptions", f"{metrics['eligible_exceptions']:,}")
    columns[1].caption("Eligible results with unresolved issues")
    columns[2].metric("Quarantined / excluded", f"{metrics['excluded_records']:,}")
    columns[2].caption(f"{counts['duplicate_key']:,} duplicate-key · {counts['invalid_key']:,} invalid-key")
    columns[3].metric("Match rate", f"{rate:.2%}" if rate is not None else "—")
    columns[3].caption(f"{matched:,} of {eligible:,} eligible")

    with st.expander(f"Source volumes · A {metrics['source_rows_a']:,} rows · B {metrics['source_rows_b']:,} rows"):
        source_a, source_b = st.columns(2)
        source_a.metric("Source A rows", f"{metrics['source_rows_a']:,}")
        source_b.metric("Source B rows", f"{metrics['source_rows_b']:,}")
    with st.expander("Match rate methodology"):
        st.write(
            "Match rate divides exact and within-tolerance matches by all eligible results. "
            "The denominator includes matches, missing counterparts, mismatches, and records with valid keys and invalid values. "
            "Duplicate-key and invalid-key records are excluded. A zero denominator yields no rate."
        )
    if not metrics["result_records"]:
        st.info("The two sources contain headers but no transaction rows to reconcile.")
        return

    totals = currency_audit(result)
    flags_column, amounts_column = st.columns([1, 1])
    with flags_column:
        _exception_flags(metrics["flag_counts"])
    with amounts_column:
        _currency_differences(totals)
    _currency_totals_table(totals)

    with st.expander("Primary classification counts"):
        st.dataframe(
            pd.DataFrame(
                [(CLASSIFICATION_LABELS[name], count) for name, count in counts.items()],
                columns=["Primary classification", "Result records"],
            ),
            hide_index=True,
            width="stretch",
        )
