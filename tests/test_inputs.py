import csv
import json
from decimal import Decimal

from openpyxl import Workbook
import pytest

from reconcileiq import ColumnMapping, ReconciliationConfig, reconcile
from reconcileiq.cli import main
from reconcileiq.inputs import InputError, load_source


MAPPING = ColumnMapping(("transaction",), "amount", "status", "currency", "reference")
CONFIG = ReconciliationConfig(MAPPING, MAPPING)
HEADERS = "transaction,amount,status,currency,reference"


def test_csv_preserves_text_and_physical_positions(tmp_path):
    path = tmp_path / "transactions.csv"
    path.write_text(
        f'{HEADERS}\n001,10.00,SETTLED,USD,"A\n100"\n\nNA,9.90,SETTLED,USD,B\n,,,,\n',
        encoding="utf-8-sig",
        newline="\n",
    )

    frame, positions = load_source(path, amount_column="amount")

    assert positions == (2, 5, 6)
    assert frame["transaction"].tolist()[:2] == ["001", "NA"]
    assert frame["reference"].tolist()[:2] == ["A\n100", "B"]
    assert frame["amount"].tolist()[:2] == ["10.00", "9.90"]


def test_csv_and_named_xlsx_worksheet_reconcile_with_exact_amounts(tmp_path):
    csv_path = tmp_path / "left.csv"
    csv_path.write_text(
        f"{HEADERS}\n"
        "001,90071992547409.92,SETTLED,USD,left-1\n"
        "002,-17.01,SETTLED,EUR,left-2\n"
        "003,0.00000001,SETTLED,USD,left-3\n",
        encoding="utf-8",
    )
    xlsx_path = tmp_path / "right.xlsx"
    book = Workbook()
    book.active.append(["Other worksheet"])
    sheet = book.create_sheet("Transactions")
    sheet.append(HEADERS.split(","))
    sheet.append(["001", "90071992547409.92", "SETTLED", "USD", "right-1"])
    sheet.append([None] * 5)
    sheet.append(["002", -17.01, "SETTLED", "EUR", "right-2"])
    sheet.append(["003", 0.00000001, "SETTLED", "USD", "right-3"])
    book.save(xlsx_path)

    a, a_rows = load_source(csv_path, amount_column="amount")
    b, b_rows = load_source(xlsx_path, sheet="Transactions", amount_column="amount")
    result = reconcile(a, b, CONFIG, row_numbers_a=a_rows, row_numbers_b=b_rows)
    records = {record.key[0]: record for record in result.records}

    assert a_rows == (2, 3, 4)
    assert b_rows == (2, 4, 5)
    assert {r.classification for r in records.values()} == {"matched"}
    assert records["001"].source_b.amount == Decimal("90071992547409.92")
    assert records["002"].source_b.amount == Decimal("-17.01")
    assert records["002"].source_b.row_number == 4
    assert records["002"].source_b.row_id == "right-2"
    assert records["003"].source_b.amount == Decimal("0.00000001")


def test_xlsx_formulas_and_error_cells_surface_invalid_values(tmp_path):
    path = tmp_path / "unsupported_cells.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.append(HEADERS.split(","))
    sheet.append(["t1", "=1+1", "SETTLED", "#N/A", "entry"])
    book.save(path)

    frame, positions = load_source(path, amount_column="amount")
    result = reconcile(frame, frame, CONFIG, row_numbers_a=positions, row_numbers_b=positions)
    record, = result.records

    assert record.classification == "invalid_value"
    assert record.flags == ("invalid_amount", "invalid_currency")
    assert record.source_a.row_number == 2
    assert record.amount_delta is None


def test_header_only_csv_is_a_valid_empty_source(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text(f"{HEADERS}\n", encoding="utf-8")

    frame, positions = load_source(path, amount_column="amount")

    assert frame.empty
    assert frame.columns.tolist() == HEADERS.split(",")
    assert positions == ()
    assert reconcile(frame, frame, CONFIG).summary["match_rate"] is None


@pytest.mark.parametrize("csv_text", ["", "id,id\n1,2\n", "id,amount\n1\n", 'id,amount\n1,"broken\n'])
def test_malformed_csv_fails_explicitly(tmp_path, csv_text):
    path = tmp_path / "bad.csv"
    path.write_text(csv_text, encoding="utf-8")

    with pytest.raises(InputError):
        load_source(path)


def test_xlsx_missing_worksheet_fails_explicitly(tmp_path):
    path = tmp_path / "workbook.xlsx"
    book = Workbook()
    book.active.append(HEADERS.split(","))
    book.save(path)

    with pytest.raises(InputError, match="Nonexistent"):
        load_source(path, sheet="Nonexistent", amount_column="amount")


def test_cli_writes_traceable_structured_outputs(tmp_path):
    left = tmp_path / "left.csv"
    left.write_text(f"{HEADERS}\n001,0.10,SETTLED,USD,left-1\n", encoding="utf-8")
    right = tmp_path / "right.csv"
    right.write_text(f"{HEADERS}\n001,0.11,FAILED,USD,right-1\n", encoding="utf-8")
    config_path = tmp_path / "mapping.json"
    mapping = {
        "keys": ["transaction"],
        "amount": "amount",
        "status": "status",
        "currency": "currency",
        "row_id": "reference",
    }
    config_path.write_text(json.dumps({"source_a": mapping, "source_b": mapping}), encoding="utf-8")
    out = tmp_path / "results"

    assert main([
        "--source-a", str(left),
        "--source-b", str(right),
        "--config", str(config_path),
        "--output-dir", str(out),
    ]) == 0

    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    record, = report["records"]
    assert record["key"] == ["001"]
    assert record["classification"] == "amount_mismatch"
    assert record["flags"] == ["amount_mismatch", "status_mismatch"]
    assert record["amount_delta"] == "-0.01"
    assert record["source_a"]["row_number"] == 2
    assert record["source_a"]["row_id"] == "left-1"
    assert record["source_b"]["row_id"] == "right-1"
    assert report["summary"]["totals_by_currency"]["USD"]["delta"] == "-0.01"
    with (out / "records.csv").open(encoding="utf-8-sig", newline="") as file:
        exported, = list(csv.DictReader(file))
    assert json.loads(exported["key"]) == ["001"]
    assert exported["classification"] == "amount_mismatch"
    assert exported["amount_delta"] == "-0.01"
    assert exported["source_a_row_number"] == "2"
    assert exported["source_b_row_id"] == "right-1"
