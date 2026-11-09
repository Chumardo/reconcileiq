from collections import Counter
from decimal import Decimal
from io import BytesIO
import json
from xml.etree import ElementTree
from zipfile import ZipFile

from openpyxl import Workbook
import pytest

from reconcileiq import ReconciliationConfig, reconcile
from reconcileiq.cli import main
from reconcileiq.inputs import InputError
from reconcileiq_ui.sources import parse_upload


MAPPING = {"keys": ["ID"], "amount": "amount", "currency": "currency", "status": "status", "row_id": "source_id"}
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def xlsx(keys, tokens=None):
    book = Workbook()
    book.active.append(["ID", "amount", "currency", "status", "source_id"])
    for row, key in enumerate(keys, start=2):
        book.active.append([key, "12.34", "USD", "PAID", f"source-{row}"])
    buffer = BytesIO()
    book.save(buffer)
    if not tokens:
        return buffer.getvalue()
    output = BytesIO()
    with ZipFile(buffer) as original, ZipFile(output, "w") as revised:
        for part in original.namelist():
            content = original.read(part)
            if part == "xl/worksheets/sheet1.xml":
                root = ElementTree.fromstring(content)
                for row, token in tokens.items():
                    cell = root.find(f".//{NS}c[@r='A{row}']/{NS}v")
                    assert cell is not None
                    cell.text = token
                content = ElementTree.tostring(root)
            revised.writestr(part, content)
    return output.getvalue()


def audit(a, b, kind):
    settings = ReconciliationConfig.from_dict({"source_a": MAPPING, "source_b": MAPPING, "key_types": [kind]})
    options = {"amount_column": "amount", "integer_key_columns": settings.source_a.integer_key_columns(settings.key_types)}
    rows_a, positions_a = parse_upload("a.xlsx", a, **options)
    rows_b, positions_b = parse_upload("b.xlsx", b, **options)
    return reconcile(rows_a, rows_b, settings, row_numbers_a=positions_a, row_numbers_b=positions_b)


def test_integer_excel_tokens_match_exactly_and_preserve_cli_evidence(tmp_path):
    a = xlsx([123, 1, 1], {3: "9007199254740993", 4: "9007199254740992"})
    b = xlsx([123, 1, 1], {2: "123.0", 3: "9.007199254740993E15", 4: "9007199254740992.0"})
    for name, data in (("a", a), ("b", b)):
        (tmp_path / f"{name}.xlsx").write_bytes(data)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"source_a": MAPPING, "source_b": MAPPING, "key_types": ["integer"]}))
    output = tmp_path / "report"
    assert main(["--source-a", str(tmp_path / "a.xlsx"), "--source-b", str(tmp_path / "b.xlsx"),
                 "--config", str(config), "--output-dir", str(output)]) == 0
    result = json.loads((output / "report.json").read_bytes())
    counts = result["summary"]["classification_counts"]
    assert counts["matched"] == sum(counts.values()) == 3
    entries = {record["key"][0]: record for record in result["records"]}
    for key, position, token in (("123", 2, "123.0"), ("9007199254740993", 3, "9.007199254740993E15"),
                                 ("9007199254740992", 4, "9007199254740992.0")):
        row = entries[key]["source_b"]
        assert (row["row_number"], row["row_id"], row["amount"]) == (position, f"source-{position}", "12.34")
        assert row["raw"]["key"] == [{"kind": "xlsx_number", "value": token}]
        assert entries[key]["source_a"]["raw"]["key"] == [int(key)]


def test_text_identifiers_keep_leading_zeros_and_do_not_match_numeric_cells():
    a = xlsx(["00123", "123", 123])
    b = xlsx(["00123", "123"])
    result = audit(a, b, "text")
    assert {record.key for record in result.records if record.classification == "matched"} == {("00123",), ("123",)}
    invalid = next(record for record in result.records if record.classification == "invalid_key")
    assert invalid.source_a.raw["key"] == (123,)
    assert invalid.source_a.row_number == 4
    assert audit(xlsx(["00123"]), xlsx([123], {2: "123.0"}), "integer").records[0].key == ("123",)


def test_fractional_invalid_and_null_keys_remain_visible():
    a = xlsx([123, None, "123.0", "=2", 1, 1, True], {2: "123.25", 6: "1E309", 7: "1E-100000"})
    result = audit(a, xlsx([]), "integer")
    by_row = {record.source_a.row_number: record for record in result.records}
    assert set(by_row) == set(range(2, 9))
    assert all(record.classification == "invalid_key" for record in by_row.values())
    assert by_row[3].flags == ("null_key",)
    assert all("invalid_key" in by_row[n].flags for n in (2, 4, 5, 6, 7, 8))
    assert by_row[2].source_a.raw["key"][0].value == "123.25"
    assert by_row[7].source_a.raw["key"][0].value == "1E-100000"


def test_malformed_nonfinite_numeric_xml_reports_input_error():
    with pytest.raises(InputError, match="nonfinite.xlsx"):
        parse_upload("nonfinite.xlsx", xlsx([1], {2: "NaN"}), integer_key_columns=("ID",))


def test_integer_normalization_quarantines_all_duplicate_rows_and_counterpart():
    result = audit(xlsx([5, 5], {3: "5.0"}), xlsx([5]), "integer")
    assert Counter(row.classification for row in result.records) == {"duplicate_key": 3}
    assert {row.key for row in result.records} == {("5",)}
    assert {(row.source_a.row_number if row.source_a else None,
             row.source_b.row_number if row.source_b else None)
            for row in result.records} == {(2, None), (3, None), (None, 2)}


def test_one_numeric_cell_can_supply_a_key_and_an_exact_amount():
    mapping = {**MAPPING, "amount": "ID"}
    settings = ReconciliationConfig.from_dict({"source_a": mapping, "source_b": mapping, "key_types": ["integer"]})
    a, _ = parse_upload("a.xlsx", xlsx([123]), amount_column="ID", integer_key_columns=("ID",))
    b, _ = parse_upload("b.xlsx", xlsx([123], {2: "123.00"}), amount_column="ID", integer_key_columns=("ID",))
    record = reconcile(a, b, settings).records[0]
    assert record.classification == "matched" and record.key == ("123",)
    assert record.source_a.amount == record.source_b.amount == Decimal("123.00")
    assert record.amount_delta == Decimal("0.00")
    assert record.source_b.raw["amount"].value == "123.00"
