from decimal import Decimal
from io import BytesIO
from zipfile import ZipFile

import pandas as pd
from openpyxl import load_workbook
import pytest

from reconcileiq import ColumnMapping, ReconciliationConfig, reconcile
from reconcileiq.audit import dashboard_metrics
from reconcileiq.audit_workbook import build_audit_workbook


_MAPPING = ColumnMapping(("key",), "amount", "status", "currency", "reference")
_SHEETS = [
    "Executive Summary", "Reconciliation Results", "Exceptions", "Duplicate Keys", "Data Quality",
    "Currency Summary", "Audit Configuration",
]


def _source(rows):
    return pd.DataFrame(rows, columns=("key", "amount", "status", "currency", "reference"))


def _table(sheet):
    headers = [cell.value for cell in sheet[5]]
    return [dict(zip(headers, cells, strict=True)) for cells in sheet.iter_rows(min_row=6)]


@pytest.fixture
def audit():
    source_a = _source([
        ("exact", "42.25", "Paid", "USD", "A-1"),
        ("tolerated", "1234567890.123456789012345678", "Paid", "USD", "A-2"),
        ("mismatch", "-20.00", "Paid", "USD", "A-3"),
        ("a-only", "-5.00", "Paid", "CAD", "A-4"),
        ("duplicate", "1.00", "Paid", "EUR", "A-5"),
        ("duplicate", "2.00", "Paid", "EUR", "A-6"),
        ("bad", "unavailable", "Paid", "GBP", "A-7"),
    ])
    source_b = _source([
        ("exact", "42.25", "Paid", "USD", "B-1"),
        ("tolerated", "1234567890.123456789012345677", "Paid", "USD", "B-2"),
        ("mismatch", "-21.11", "Paid", "USD", "B-3"),
        ("b-only", "0.01", "Paid", "JPY", "B-4"),
        ("duplicate", "1.00", "Paid", "EUR", "B-5"),
        ("bad", "2.00", "Paid", "GBP", "B-6"),
    ])
    config = ReconciliationConfig(_MAPPING, _MAPPING, amount_tolerance=Decimal("0.01"))
    result = reconcile(source_a, source_b, config, row_numbers_a=range(20, 27), row_numbers_b=range(30, 36))
    payload = build_audit_workbook(result, config, source_a="ledger.xlsx", source_b="bank.csv", sheet_a="July")
    workbook = load_workbook(BytesIO(payload))
    try:
        yield result, workbook
    finally:
        workbook.close()


def test_workbook_layout_and_coverage_metrics_agree_with_ui(audit):
    result, workbook = audit
    assert workbook.sheetnames == _SHEETS
    for sheet in workbook:
        assert sheet.freeze_panes.endswith("6")
        assert sheet.auto_filter.ref.startswith("A5:")
        assert sheet.sheet_view.showGridLines is False
        assert sheet["A5"].font.bold is True
        assert sheet.column_dimensions["A"].width > 12

    metrics = dashboard_metrics(result)
    coverage = {
        row["Measure"].value: row["Value"].value
        for row in _table(workbook["Executive Summary"]) if row["Category"].value == "Coverage"
    }
    assert coverage == {
        "Match rate": pytest.approx(metrics["match_rate"]),
        "Matched": metrics["matched_records"],
        "Exact matches": metrics["exact_matched_records"],
        "Matches within tolerance": metrics["tolerated_matched_records"],
        "Eligible exceptions": metrics["eligible_exceptions"],
        "Quarantined / excluded": metrics["excluded_records"],
        "Match-rate denominator": metrics["eligible_records"],
        "Amount tolerance": 0.01,
    }
    assert coverage["Match-rate denominator"] == 6
    assert coverage["Matched"] == 2
    assert coverage["Quarantined / excluded"] == 3
    assert workbook["Executive Summary"]["C6"].number_format == "0.00%"
    assert len(_table(workbook["Reconciliation Results"])) == len(result.records)


def test_workbook_currency_and_detail_amounts_keep_exact_decimals(audit):
    result, workbook = audit
    currencies = {row["Currency"].value: row for row in _table(workbook["Currency Summary"])}
    usd = currencies["USD"]
    assert usd["Source A total"].value == "1234567912.373456789012345678"
    assert usd["Source B total"].value == "1234567911.263456789012345677"
    assert usd["Source A − B"].value == "1.110000000000000001"
    assert usd["Source A − B"].data_type == "s"
    assert usd["Source A − B"].number_format == "@"
    assert usd["Comparable pairs"].value == 3
    assert usd["Matched pairs"].value == 2
    assert usd["Matched A − B"].value == 1e-18
    assert currencies["CAD"]["B currency rows"].value == 0
    assert currencies["JPY"]["A currency rows"].value == 0
    for currency, totals in result.summary["totals_by_currency"].items():
        assert Decimal(str(currencies[currency]["Source A total"].value)) == totals["source_a"]
        assert Decimal(str(currencies[currency]["Source B total"].value)) == totals["source_b"]
        assert Decimal(str(currencies[currency]["Source A − B"].value)) == totals["delta"]

    detail = {row["A row ID"].value: row for row in _table(workbook["Exceptions"])}
    tolerated = detail["A-2"]
    assert tolerated["Primary classification"].value == "matched_within_tolerance"
    assert tolerated["Issue flags"].value == "amount_within_tolerance"
    assert tolerated["A amount"].value == "1234567890.123456789012345678"
    assert Decimal(str(tolerated["A − B"].value)) == Decimal("0.000000000000000001")
    assert tolerated["A amount"].data_type == "s"
    assert tolerated["A file"].value == "ledger.xlsx"
    assert tolerated["A worksheet"].value == "July"
    assert tolerated["A file row"].value == 21
    assert tolerated["B file row"].value == 31
    missing = detail["A-4"]
    assert missing["Primary classification"].value == "missing_in_source_b"
    assert missing["Unmatched amount"].value == -5
    assert missing["Unmatched amount"].number_format == "#,##0.00"
    assert missing["A − B"].value is None
    assert missing["Amount comparison"].value == "Counterpart missing"


def test_duplicate_sheet_preserves_group_rows_from_both_sources(audit):
    _, workbook = audit
    duplicate_rows = _table(workbook["Duplicate Keys"])
    assert [row["Source"].value for row in duplicate_rows] == ["A", "A", "B"]
    assert [row["Row Id"].value for row in duplicate_rows] == ["A-5", "A-6", "B-5"]
    assert all(row["Reconciliation key"].value == '["duplicate"]' for row in duplicate_rows)
    assert all((row["Rows in A"].value, row["Rows in B"].value) == (2, 1) for row in duplicate_rows)
    detail_ids = {row["Result ID"].value for row in _table(workbook["Reconciliation Results"])}
    assert {row["Result ID"].value for row in duplicate_rows} <= detail_ids


def test_export_never_interprets_user_text_as_formulas_errors_or_hyperlinks():
    formula = '=HYPERLINK("https://example.com/", "click")'
    source_a = _source([(formula, "+cmd|' /C calc'!A0", "#REF!", "@USD", formula)])
    source_b = _source([(formula, "12.34", "=2+2", "USD", "https://example.com/")])
    config = ReconciliationConfig(_MAPPING, _MAPPING)
    result = reconcile(source_a, source_b, config)
    payload = build_audit_workbook(result, config, source_a="=SOURCE()", source_b="+bank.csv", sheet_a="-sheet")

    with ZipFile(BytesIO(payload)) as archive:
        assert all("externalLink" not in filename for filename in archive.namelist())
    workbook = load_workbook(BytesIO(payload))
    try:
        row = _table(workbook["Exceptions"])[0]
        for field, expected in {
            "A file": "=SOURCE()", "A worksheet": "-sheet", "A row ID": formula,
            "A raw amount": "+cmd|' /C calc'!A0", "A raw currency": "@USD", "A status": "#REF!",
            "B status": "=2+2", "B row ID": "https://example.com/",
        }.items():
            assert row[field].value == expected
            assert row[field].data_type == "s"
        for sheet in workbook:
            for cells in sheet.iter_rows():
                for cell in cells:
                    assert cell.data_type not in {"f", "e"}
                    assert cell.hyperlink is None
    finally:
        workbook.close()


def test_export_marks_overlong_utf16_text_and_escapes_xml_controls():
    source_a = _source([("bad", "3.50", "Paid\x01", "USD", "🙂" * 17_000)])
    config = ReconciliationConfig(_MAPPING, _MAPPING)
    result = reconcile(source_a, _source([]), config)
    workbook = load_workbook(BytesIO(build_audit_workbook(result, config, source_a="a.csv", source_b="b.csv")))
    try:
        row = _table(workbook["Exceptions"])[0]
        assert row["A status"].value == r"Paid\u0001"
        value = row["A row ID"].value
        assert len(value.encode("utf-16-le")) // 2 <= 32_767
        assert value.endswith("[truncated]")
    finally:
        workbook.close()


def test_empty_audit_has_headers_and_no_numeric_match_rate():
    config = ReconciliationConfig(_MAPPING, _MAPPING)
    result = reconcile(_source([]), _source([]), config)
    workbook = load_workbook(BytesIO(build_audit_workbook(result, config, source_a="a.csv", source_b="b.csv")))
    try:
        assert workbook["Executive Summary"]["C6"].value is None
        assert workbook["Reconciliation Results"]["A5"].value == "Result ID"
        assert not _table(workbook["Reconciliation Results"])
        assert not _table(workbook["Currency Summary"])
    finally:
        workbook.close()
