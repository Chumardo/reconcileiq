from io import BytesIO
import json
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
import pytest

from reconcileiq.cli import main
from reconcileiq.inputs import InputError
from reconcileiq.preflight import ValidatedUpload
from reconcileiq_ui.sources import UploadLimits, list_worksheets, parse_upload


_SHEET = "xl/worksheets/sheet1.xml"


def _workbook(cell="A2"):
    book = Workbook()
    sheet = book.active
    sheet.append(["ID", "Amount"])
    sheet[cell] = "row"
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


def _replace_part(content, name, transform):
    result = BytesIO()
    with ZipFile(BytesIO(content)) as source, ZipFile(result, "w", ZIP_DEFLATED) as target:
        for part in source.namelist():
            raw = source.read(part)
            target.writestr(part, transform(raw) if part == name else raw)
    return result.getvalue()


def _with_parts(content, parts):
    result = BytesIO(content)
    with ZipFile(result, "a", ZIP_DEFLATED) as archive:
        for name, data in parts:
            archive.writestr(name, data)
    return result.getvalue()


def _compressed_part():
    return _with_parts(_workbook(), [("xl/media/high-ratio.bin", b"A" * (2 * 1024**2))])


def _cli_arguments(tmp_path, content):
    workbook = tmp_path / "supplier.xlsx"
    workbook.write_bytes(content)
    config = tmp_path / "mapping.json"
    mapping = {"keys": ["ID"], "amount": "Amount", "status": "Status", "currency": "Currency"}
    config.write_text(json.dumps({"source_a": mapping, "source_b": mapping}), encoding="utf-8")
    return [
        "--source-a", str(workbook),
        "--source-b", str(workbook),
        "--config", str(config),
        "--output-dir", str(tmp_path / "results"),
    ]


@pytest.mark.parametrize("read", [list_worksheets, parse_upload])
def test_upload_bytes_limit_is_inclusive_and_validated_at_both_entry_points(read):
    content = b"ID\nx\n"

    assert read("exact.csv", content, limits=UploadLimits(max_bytes=len(content))) is not None
    with pytest.raises(InputError, match="large.csv.*upload size limit"):
        read("large.csv", content, limits=UploadLimits(max_bytes=len(content) - 1))


@pytest.mark.parametrize(
    "content, message",
    [
        (b"ID\nabc\x00\n", "without binary or NUL bytes"),
        (b"ID\n\xff\n", "utf-8"),
        (b'ID,Amount\n"unterminated,2\n', "Malformed CSV record at line 2"),
        (b"ID,Amount\nx,1,unexpected\n", "CSV line 2 has 3 fields"),
    ],
)
def test_invalid_csv_upload_explains_why_it_failed(content, message):
    with pytest.raises(InputError, match=message):
        parse_upload("bad.csv", content)


@pytest.mark.parametrize(
    "limits, message",
    [
        (UploadLimits(max_rows=1), "1-data-row limit"),
        (UploadLimits(max_columns=1), "1-column limit"),
        (UploadLimits(max_cells=3), "3-data-cell limit"),
    ],
)
def test_csv_limits_count_data_records_and_columns(limits, message):
    content = b"ID,Amount\nx,1\n\ny,2\n"
    with pytest.raises(InputError, match=message):
        parse_upload("oversized.csv", content, limits=limits)

    frame, rows = parse_upload("fits.csv", content, limits=UploadLimits(max_rows=2, max_columns=2, max_cells=4))
    assert frame.shape == (2, 2)
    assert rows == (2, 4)


def test_xlsx_upload_rejects_other_formats_and_bounded_zip_expansion():
    content = _workbook()
    with pytest.raises(InputError, match="CSV file"):
        parse_upload("workbook.csv", content)
    with pytest.raises(InputError, match="unsupported.txt"):
        list_worksheets("unsupported.txt", content)
    with pytest.raises(InputError, match="zip"):
        parse_upload("text.xlsx", b"ID\nx\n")
    with pytest.raises(InputError, match="expanded size limit"):
        list_worksheets("large.xlsx", content, limits=UploadLimits(max_expanded_bytes=100))
    with pytest.raises(InputError, match="ZIP-entry limit"):
        parse_upload("many-parts.xlsx", content, limits=UploadLimits(max_zip_entries=1))


@pytest.mark.parametrize(
    "cell,limits,message",
    [
        ("A11", UploadLimits(max_rows=5), "5-data-row limit"),
        ("D2", UploadLimits(max_columns=3), "3-column limit"),
        ("A5", UploadLimits(max_cells=7), "7-data-cell limit"),
    ],
)
def test_sparse_worksheet_limits_apply_before_workbook_read(cell, limits, message):
    with pytest.raises(InputError, match=message):
        list_worksheets("sparse.xlsx", _workbook(cell), limits=limits)


def test_xlsx_rejects_dtds_and_malformed_xml():
    valid = _workbook()
    dtd = _replace_part(valid, _SHEET, lambda raw: b'<!DOCTYPE worksheet [<!ENTITY test "abc">]>' + raw)
    malformed = _replace_part(valid, _SHEET, lambda raw: raw[:-7])

    with pytest.raises(InputError, match="prohibited DTD or entity declaration"):
        parse_upload("dtd.xlsx", dtd)
    with pytest.raises(InputError, match="malformed.xlsx"):
        list_worksheets("malformed.xlsx", malformed)


def test_xlsx_rejects_unknown_xml_encoding_with_upload_error():
    content = _replace_part(
        _workbook(), _SHEET, lambda raw: b'<?xml version="1.0" encoding="unknown-codec"?>' + raw,
    )
    with pytest.raises(InputError, match="unknown encoding"):
        parse_upload("bad-encoding.xlsx", content)


def test_valid_workbook_uses_the_same_input_policy_in_cli_and_private_ui(tmp_path):
    workbook = Workbook()
    workbook.active.append(["ID", "Amount", "Status", "Currency"])
    workbook.active.append(["001", "10.00", "settled", "USD"])
    buffer = BytesIO()
    workbook.save(buffer)
    content = buffer.getvalue()

    validated = ValidatedUpload("supplier.xlsx", content, UploadLimits())
    assert validated.suffix == ".xlsx"
    frame, locators = parse_upload("supplier.xlsx", content)
    assert frame["ID"].tolist() == ["001"]
    assert locators == (2,)
    assert main(_cli_arguments(tmp_path, content)) == 0
    report = json.loads((tmp_path / "results" / "report.json").read_text(encoding="utf-8"))
    assert report["records"][0]["classification"] == "matched"
    assert report["records"][0]["source_a"]["row_number"] == 2


@pytest.mark.parametrize(
    "content_factory, message",
    [
        (lambda: b"x" * (15 * 1024**2 + 1), "upload size limit"),
        (lambda: _with_parts(_workbook(), (
            (f"xl/media/part-{index}.bin", b"") for index in range(1_024)
        )), "ZIP-entry limit"),
        (_compressed_part, "1,000:1 expansion ratio limit"),
        (lambda: _workbook("A100002"), "100,000-data-row limit"),
        (lambda: _workbook()[:-32], "zip file"),
    ],
)
def test_cli_and_private_ui_reject_unsafe_xlsx_before_loading(
    tmp_path, capsys, monkeypatch, content_factory, message,
):
    def unexpected_loader(*args, **kwargs):
        pytest.fail("openpyxl was called before the XLSX preflight rejected the file")

    monkeypatch.setattr("reconcileiq.inputs.load_workbook", unexpected_loader)
    content = content_factory()
    with pytest.raises(InputError, match=message):
        parse_upload("supplier.xlsx", content)

    with pytest.raises(SystemExit) as error:
        main(_cli_arguments(tmp_path, content))
    assert error.value.code == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "results").exists()


def test_xlsx_preflight_keeps_expanded_size_limit_and_allows_small_compressible_parts():
    content = _with_parts(_workbook(), [("xl/media/small.bin", b"A" * (512 * 1024))])
    assert list_worksheets("supplier.xlsx", content) == ("Sheet",)

    with pytest.raises(InputError, match="expanded size limit"):
        list_worksheets("supplier.xlsx", content, limits=UploadLimits(max_expanded_bytes=256 * 1024))
