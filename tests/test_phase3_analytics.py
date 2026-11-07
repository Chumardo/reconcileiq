from decimal import Decimal, localcontext

import pandas as pd
import pytest

from reconcileiq import ColumnMapping, ReconciliationConfig, reconcile
from reconcileiq.audit import currency_audit, issue_impact, unmatched_amount
from reconcileiq_ui.analysis import dashboard_metrics, duplicate_group, filter_exceptions


MAPPING = ColumnMapping(("txn",), "amount", "status", "currency", "reference")


def _frame(rows):
    return pd.DataFrame(rows, columns=MAPPING.required_columns, dtype=object)


@pytest.fixture
def report():
    a = _frame([
        ("exact", "100.00", "SETTLED", "USD", "A-1"),
        ("tolerated", "2.04", "SETTLED", "USD", "A-2"),
        ("status", "10.00", "SETTLED", "USD", "A-3"),
        ("amount", "12.00", "SETTLED", "USD", "A-4"),
        ("only-a", "-3.00", "SETTLED", "USD", "A-5"),
        ("repeat", "1.00", "SETTLED", "USD", "A-6"),
        ("repeat", "2.00", "SETTLED", "EUR", "A-7"),
        ("currency", "4.00", "SETTLED", "GBP", "A-8"),
        ("only-a-invalid", "pending", "SETTLED", "NOK", "A-9"),
        ("bad-amount", "pending", "SETTLED", "CHF", "A-10"),
    ])
    b = _frame([
        ("exact", "100.00", "SETTLED", "USD", "B-1"),
        ("tolerated", "2.00", "SETTLED", "USD", "B-2"),
        ("status", "10.00", "HELD", "USD", "B-3"),
        ("amount", "11.95", "SETTLED", "USD", "B-4"),
        ("only-b", "1.05", "SETTLED", "EUR", "B-5"),
        ("repeat", "3.00", "SETTLED", "CAD", "B-6"),
        ("currency", "4.00", "SETTLED", "EUR", "B-7"),
        ("bad-amount", "pending", "SETTLED", "CHF", "B-8"),
    ])
    return reconcile(a, b, ReconciliationConfig(MAPPING, MAPPING, amount_tolerance="0.04"))


def test_currency_totals_separate_source_compared_and_matched_money(report):
    totals = {row.currency: row for row in currency_audit(report)}
    usd = totals["USD"]
    assert (usd.source_a, usd.source_b, usd.source_delta) == tuple(map(Decimal, ("122.04", "123.95", "-1.91")))
    assert (usd.compared_a, usd.compared_b, usd.compared_delta) == tuple(map(Decimal, ("124.04", "123.95", "0.09")))
    assert (usd.matched_a, usd.matched_b, usd.matched_delta) == tuple(map(Decimal, ("102.04", "102.00", "0.04")))
    assert (usd.amount_rows_a, usd.amount_rows_b, usd.compared_pairs, usd.matched_pairs) == (6, 4, 4, 2)

    assert totals["CAD"].coverage == "Missing in Source A"
    assert totals["GBP"].coverage == "Missing in Source B"
    assert totals["CHF"].coverage == "No valid amounts in either source"
    assert totals["CHF"].currency_rows_a == totals["CHF"].currency_rows_b == 1
    assert totals["EUR"].compared_pairs == totals["CAD"].compared_pairs == totals["GBP"].compared_pairs == 0
    for code, engine in report.summary["totals_by_currency"].items():
        row = totals[code]
        assert (row.source_a, row.source_b, row.source_delta) == (engine["source_a"], engine["source_b"], engine["delta"])


def test_currency_totals_keep_large_decimal_deltas_at_low_ambient_precision():
    a = _frame([("tolerated", "99999999999999999999999999999999999999.000000000000000001", "SETTLED", "USD", "A-1")])
    b = _frame([("tolerated", "99999999999999999999999999999999999999.000000000000000000", "SETTLED", "USD", "B-1")])
    with localcontext() as context:
        context.prec = 6
        result = reconcile(a, b, ReconciliationConfig(MAPPING, MAPPING, amount_tolerance="0.000000000000000001"))
        row, = currency_audit(result)
    assert row.source_delta == row.compared_delta == row.matched_delta == Decimal("0.000000000000000001")


def test_duplicate_group_includes_rows_hidden_by_a_currency_filter(report):
    filtered = filter_exceptions(report.records, currencies=("USD",), query="repeat")
    selected, = filtered
    group = duplicate_group(report.records, selected.key)
    assert [record.source_a.row_id if record.source_a else record.source_b.row_id for record in group] == ["A-6", "A-7", "B-6"]
    assert all(issue_impact(record) == "High" and unmatched_amount(record) is None for record in group)


def test_missing_amount_preserves_sign_and_requires_a_valid_amount_and_currency(report):
    records = {record.key: record for record in report.records}
    missing_a, missing_b = records[("only-b",)], records[("only-a",)]
    assert missing_a.classification == "missing_in_source_a"
    assert missing_b.classification == "missing_in_source_b"
    assert unmatched_amount(missing_a) == Decimal("1.05")
    assert unmatched_amount(missing_b) == Decimal("-3.00")
    assert missing_a.amount_delta is missing_b.amount_delta is None
    assert unmatched_amount(records[("only-a-invalid",)]) is None
    assert issue_impact(missing_a) == "High"
    assert issue_impact(records[("currency",)]) == "High"
    assert issue_impact(records[("amount",)]) == issue_impact(records[("status",)]) == "Review"


def test_overview_metrics_use_exact_and_tolerated_matches_in_the_numerator(report):
    metrics = dashboard_metrics(report)
    assert metrics["matched_records"] == 2
    assert metrics["exact_matched_records"] == metrics["tolerated_matched_records"] == 1
    assert metrics["eligible_records"] == 9
    assert metrics["eligible_exceptions"] == 7
    assert metrics["excluded_records"] == 3
    assert metrics["match_rate"] == pytest.approx(2 / 9)
