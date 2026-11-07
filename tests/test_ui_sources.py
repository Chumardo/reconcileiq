from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook
import pytest

from reconcileiq import ConfigurationError
from reconcileiq.inputs import InputError
from reconcileiq_ui.sources import list_worksheets, parse_upload, validate_mapping


A_COLUMNS = ["merchant", "txn", "amount", "status", "currency", "ledger_row"]
B_COLUMNS = ["account", "payment", "net", "state", "ccy", "statement_row"]


def mapping():
    return {
        "source_a": {
            "keys": ["merchant", "txn"],
            "amount": "amount",
            "status": "status",
            "currency": "currency",
            "row_id": "ledger_row",
        },
        "source_b": {
            "keys": ["account", "payment"],
            "amount": "net",
            "status": "state",
            "currency": "ccy",
            "row_id": "statement_row",
        },
        "key_types": ["text", "integer"],
    }


def test_csv_upload_keeps_text_amounts_ids_and_physical_positions():
    content = (
        "\ufeffmerchant,txn,amount,status,currency,ledger_row\n"
        "M001,0001,90071992547409.92,PAID,USD,A1\n"
        "\n"
        "M001,0002,-0.01,VOID,EUR,A2\n"
    ).encode("utf-8")

    assert list_worksheets("ledger.CSV", content) == ()
    frame, rows = parse_upload("ledger.CSV", content, amount_column="amount")

    assert rows == (2, 4)
    assert frame.columns.tolist() == A_COLUMNS
    assert frame["txn"].tolist() == ["0001", "0002"]
    assert frame["amount"].tolist() == ["90071992547409.92", "-0.01"]


def test_xlsx_upload_selects_sheet_and_preserves_native_decimal_amount():
    workbook = Workbook()
    workbook.active.title = "Instructions"
    workbook.active.append(["Read me"])
    transactions = workbook.create_sheet("Bank feed")
    transactions.append(B_COLUMNS)
    transactions.append(["M001", "0001", 0.00000001, "PAID", "USD", "B1"])
    transactions.append([None] * len(B_COLUMNS))
    transactions.append(["M001", "0002", "90071992547409.92", "PAID", "EUR", "B2"])
    stream = BytesIO()
    workbook.save(stream)
    content = stream.getvalue()

    assert list_worksheets("bank.xlsx", content) == ("Instructions", "Bank feed")
    frame, rows = parse_upload("bank.xlsx", content, sheet="Bank feed", amount_column="net")

    assert rows == (2, 4)
    assert frame.columns.tolist() == B_COLUMNS
    assert frame["payment"].tolist() == ["0001", "0002"]
    assert frame["net"].tolist() == [Decimal("0.00000001"), "90071992547409.92"]


def test_header_only_upload_keeps_its_schema():
    frame, rows = parse_upload("empty.csv", (",".join(A_COLUMNS) + "\n").encode())

    assert frame.empty
    assert frame.columns.tolist() == A_COLUMNS
    assert rows == ()


@pytest.mark.parametrize("filename,content", [("empty.csv", b""), ("bad.xlsx", b"invalid"), ("file.txt", b"x")])
def test_upload_validation_surfaces_invalid_files(filename, content):
    with pytest.raises(InputError):
        parse_upload(filename, content)


def test_mapping_builds_ordered_composite_keys_with_different_headers():
    config = validate_mapping(mapping(), A_COLUMNS, B_COLUMNS)

    assert config.source_a.keys == ("merchant", "txn")
    assert config.source_b.keys == ("account", "payment")
    assert config.key_types == ("text", "integer")
    assert config.source_a.row_id == "ledger_row"
    assert config.source_b.row_id == "statement_row"


def test_mapping_validation_reports_missing_required_column():
    with pytest.raises(ConfigurationError, match="Source B.*net"):
        validate_mapping(mapping(), A_COLUMNS, [column for column in B_COLUMNS if column != "net"])


def test_mapping_validation_rejects_different_key_arity():
    data = mapping()
    data["source_b"]["keys"] = ["payment"]

    with pytest.raises(ConfigurationError):
        validate_mapping(data, A_COLUMNS, B_COLUMNS)
