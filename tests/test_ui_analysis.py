from decimal import Decimal
from xml.etree import ElementTree

import pandas as pd
import pytest

from reconcileiq import ColumnMapping, ReconciliationConfig, reconcile
from reconcileiq_ui.analysis import (
    currency_totals,
    dashboard_metrics,
    filter_exceptions,
    quality_by_source,
)


MAPPING = ColumnMapping(("txn",), "amount", "status", "currency", "reference")
CONFIG = ReconciliationConfig(MAPPING, MAPPING)


def frame(rows=(), mapping=MAPPING):
    return pd.DataFrame(rows, columns=mapping.required_columns, dtype=object)


@pytest.fixture
def audit_result():
    a = frame([
        ("paid", "0.10", "SETTLED", "USD", "Ledger-01"),
        ("dual", "0.30", "SETTLED", "USD", "Ledger-02"),
        ("only-a", "2.05", "SETTLED", "EUR", "Ledger-03"),
        ("duplicate", "1.00", "SETTLED", "USD", "Ledger-04"),
        ("duplicate", "2.00", "SETTLED", "USD", "Ledger-05"),
        (None, "3.00", "SETTLED", "EUR", "Ledger-06"),
        ("bad-amount", "", "SETTLED", "USD", "Ledger-07"),
        ("bad-currency", "7.00", "", "", "Ledger-08"),
        ("wrong-unit", "0.20", "SETTLED", "USD", "Ledger-09"),
    ])
    b = frame([
        ("paid", "0.10", "SETTLED", "USD", "Bank-01"),
        ("dual", "0.25", "FAILED", "USD", "Bank-02"),
        ("only-b", "1.05", "SETTLED", "EUR", "Bank-03"),
        ("duplicate", "3.00", "SETTLED", "USD", "Bank-04"),
        ("bad-amount", "4.00", "SETTLED", "USD", "Bank-05"),
        ("bad-currency", "7.00", "SETTLED", None, "Bank-06"),
        ("wrong-unit", "0.19", "SETTLED", "CAD", "Bank-07"),
    ])
    return reconcile(a, b, CONFIG)


def test_dashboard_metrics_separate_records_eligibility_and_issue_flags(audit_result):
    metrics = dashboard_metrics(audit_result)

    assert (metrics["source_rows_a"], metrics["source_rows_b"]) == (9, 7)
    assert metrics["result_records"] == 11
    assert metrics["eligible_records"] == 7
    assert metrics["excluded_records"] == 4
    assert metrics["matched_records"] == 1
    assert metrics["eligible_exceptions"] == 6
    assert metrics["match_rate"] == pytest.approx(1 / 7)
    assert sum(metrics["classification_counts"].values()) == metrics["result_records"]
    assert metrics["classification_counts"]["status_mismatch"] == 0
    assert metrics["flag_counts"]["status_mismatch"] == 1
    assert metrics["flag_counts"]["amount_mismatch"] == 1


def test_quality_metrics_distinguish_repeated_and_quarantined_counterpart_rows(audit_result):
    quality = quality_by_source(audit_result)
    a, b = quality["source_a"], quality["source_b"]

    assert (a["source_rows"], b["source_rows"]) == (9, 7)
    assert (a["duplicate_keys"], b["duplicate_keys"]) == (1, 0)
    assert (a["duplicate_key_rows"], b["duplicate_key_rows"]) == (2, 0)
    assert (a["duplicate_quarantined_rows"], b["duplicate_quarantined_rows"]) == (2, 1)
    assert (a["null_key"], b["null_key"]) == (1, 0)
    assert (a["invalid_amount"], b["invalid_amount"]) == (1, 0)
    assert (a["invalid_status"], b["invalid_status"]) == (1, 0)
    assert (a["invalid_currency"], b["invalid_currency"]) == (1, 1)
    assert (a["missing_key"], b["missing_key"]) == (1, 0)
    assert (a["missing_amount"], b["missing_amount"]) == (1, 0)
    assert (a["missing_status"], b["missing_status"]) == (1, 0)
    assert (a["missing_currency"], b["missing_currency"]) == (1, 1)
    assert (a["rows_missing_required"], b["rows_missing_required"]) == (3, 1)
    assert (a["excluded_rows"], b["excluded_rows"]) == (3, 1)
    assert (a["amounts_excluded"], b["amounts_excluded"]) == (2, 1)


def test_issue_matrix_renders_exact_audit_counts_sorted_by_total_then_label(audit_result):
    pytest.importorskip("streamlit")
    from reconcileiq_ui.quality import _issue_matrix

    matrix = ElementTree.fromstring(_issue_matrix(quality_by_source(audit_result)))
    assert [cell.text for cell in matrix.findall("./table/thead/tr/th")] == [
        "Issue", "Source A", "Source B", "Total",
    ]
    assert [tuple(cell.text for cell in row) for row in matrix.findall("./table/tbody/tr")] == [
        ("Invalid currency", "1", "1", "2"),
        ("Rows with repeated keys", "2", "—", "2"),
        ("Invalid amount", "1", "—", "1"),
        ("Invalid status", "1", "—", "1"),
        ("Null or blank key", "1", "—", "1"),
        ("Invalid key type", "—", "—", "—"),
    ]


def test_currency_totals_are_separate_and_decimal_safe(audit_result):
    totals = currency_totals(audit_result)

    assert totals.columns.tolist() == ["Currency", "Source A", "Source B", "A − B", "Rows in A", "Rows in B"]
    assert set(totals["Currency"]) == {"CAD", "EUR", "USD"}
    amounts = totals.set_index("Currency")
    for column in ["Source A", "Source B", "A − B"]:
        assert all(isinstance(value, Decimal) for value in amounts[column])
    assert tuple(amounts.loc["USD"]) == (Decimal("3.60"), Decimal("7.35"), Decimal("-3.75"), 5, 4)
    assert tuple(amounts.loc["EUR"]) == (Decimal("5.05"), Decimal("1.05"), Decimal("4.00"), 2, 1)
    assert tuple(amounts.loc["CAD"]) == (Decimal("0"), Decimal("0.19"), Decimal("-0.19"), 0, 1)


def test_filter_exceptions_combines_filter_dimensions(audit_result):
    records = tuple(record for record in audit_result.records if record.classification != "matched")
    assert len(filter_exceptions(records)) == 10
    assert filter_exceptions(records, classifications=("matched",)) == ()

    selected = filter_exceptions(records, classifications=("amount_mismatch", "currency_mismatch"))
    assert {record.key for record in selected} == {("dual",), ("wrong-unit",)}

    selected = filter_exceptions(
        records,
        classifications=("amount_mismatch", "currency_mismatch"),
        flags=("status_mismatch", "invalid_amount"),
        currencies=("USD",),
    )
    assert [record.key for record in selected] == [("dual",)]


def test_filter_exceptions_checks_both_currencies_and_key_text(audit_result):
    records = audit_result.records
    assert [record.key for record in filter_exceptions(records, currencies=("CAD",))] == [("wrong-unit",)]
    assert [record.key for record in filter_exceptions(records, currencies=("",))] == [("bad-currency",)]
    assert [record.key for record in filter_exceptions(records, query="ONLY-B")] == [("only-b",)]
    assert filter_exceptions(records, query="unknown") == ()


def test_filter_finds_raw_components_of_unusable_composite_keys():
    mapping = ColumnMapping(("account", "txn"), "amount", "status", "currency", "reference")
    a = frame([("Acct-Special", None, "0.10", "SETTLED", "USD", "Ledger-01")], mapping)
    result = reconcile(a, frame(mapping=mapping), ReconciliationConfig(mapping, mapping))

    record, = filter_exceptions(result.records, query="acct-special")
    assert record.key is None
    assert record.classification == "invalid_key"


def test_empty_results_keep_zero_counts_and_undefined_match_rate():
    result = reconcile(frame(), frame(), CONFIG)
    metrics = dashboard_metrics(result)

    assert metrics["result_records"] == 0
    assert metrics["eligible_records"] == 0
    assert metrics["eligible_exceptions"] == 0
    assert metrics["match_rate"] is None
    assert filter_exceptions(result.records) == ()
    assert currency_totals(result).empty
    assert all(source["source_rows"] == 0 for source in quality_by_source(result).values())
