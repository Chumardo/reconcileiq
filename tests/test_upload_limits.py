from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
import pytest

from reconcileiq.inputs import InputError
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
