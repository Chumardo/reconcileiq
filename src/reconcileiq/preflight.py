"""Check file and workbook limits before loading transaction tables."""

from collections.abc import Callable
from dataclasses import dataclass, field, fields
from io import BytesIO
from pathlib import PurePosixPath
import re
from xml.parsers import expat
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile
from zlib import error as DecompressionError

_MIB = 1024 ** 2
_CSV_CONTROL_BYTES = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CELL_REFERENCE = re.compile(r"([A-Za-z]{1,3})([1-9][0-9]{0,6})")
_ROW_REFERENCE = re.compile(r"[1-9][0-9]{0,6}")
_XML_SUFFIXES = (".xml", ".rels", ".vml")
_WORKBOOK_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_BINARY_RELATIONSHIPS = {"audio", "image", "oleObject", "package", "printerSettings", "video"}
_WORKSHEET_ROW = "http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"


class InputError(ValueError):
    """A source file cannot be read as the expected transaction table."""


@dataclass(frozen=True, slots=True)
class UploadLimits:
    max_bytes: int = 15 * _MIB
    max_expanded_bytes: int = 96 * _MIB
    max_rows: int = 100_000
    max_columns: int = 128
    max_cells: int = 2_000_000
    max_zip_entries: int = 1_024
    max_expansion_ratio: int = 1_000

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{field.name} must be a positive integer")


def _column_number(letters: str) -> int:
    column = 0
    for letter in letters.upper():
        column = column * 26 + ord(letter) - ord("A") + 1
    return column


class _WorksheetExtent:
    """Track real coordinates since workbook dimension metadata may be inaccurate."""

    def __init__(self, limits: UploadLimits):
        self.limits = limits
        self.row = 0
        self.column = 0
        self.width = 0

    def start_row(self, reference: str | None) -> None:
        if reference is not None and not _ROW_REFERENCE.fullmatch(reference):
            raise InputError("Workbook contains an invalid worksheet row number.")
        row = int(reference) if reference is not None else self.row + 1
        if row <= self.row:
            raise InputError("Workbook worksheet rows must appear in increasing order.")
        if row > self.limits.max_rows + 1:
            raise InputError(f"Workbook worksheet exceeds the {self.limits.max_rows:,}-data-row limit.")
        self.row = row
        self.column = 0
        self._check_area()

    def add_cell(self, reference: str | None) -> None:
        if reference is None:
            column = self.column + 1
        else:
            match = _CELL_REFERENCE.fullmatch(reference)
            if match is None or int(match[2]) != self.row:
                raise InputError("Workbook contains an invalid worksheet cell reference.")
            column = _column_number(match[1])
        if column <= self.column:
            raise InputError("Workbook worksheet columns must appear in increasing order within a row.")
        if column > self.limits.max_columns:
            raise InputError(f"Workbook worksheet exceeds the {self.limits.max_columns:,}-column limit.")
        self.column = column
        self.width = max(self.width, column)
        self._check_area()

    def _check_area(self) -> None:
        if (self.row - 1) * self.width > self.limits.max_cells:
            raise InputError(f"Workbook worksheet exceeds the {self.limits.max_cells:,}-data-cell limit.")


def _parse_xml(archive: ZipFile, name: str, on_start: Callable[[list[str], dict[str, str]], None]) -> None:
    parser = expat.ParserCreate(namespace_separator="}")
    stack: list[str] = []

    def start_element(tag: str, attrs: dict[str, str]) -> None:
        stack.append(tag)
        if len(stack) > 128:
            raise InputError("Workbook XML is nested too deeply.")
        on_start(stack, attrs)

    def reject_doctype(*_args) -> None:
        raise InputError("Workbook XML contains a prohibited DTD or entity declaration.")

    parser.StartElementHandler = start_element
    parser.EndElementHandler = lambda _tag: stack.pop()
    parser.StartDoctypeDeclHandler = reject_doctype
    with archive.open(name) as stream:
        while chunk := stream.read(64 * 1024):
            parser.Parse(chunk, False)
        parser.Parse(b"", True)


def _workbook_part(archive: ZipFile) -> PurePosixPath:
    workbooks = []
    names = set(archive.namelist())

    def inspect_type(stack: list[str], attrs: dict[str, str]) -> None:
        if len(stack) != 2 or stack[0].rsplit("}", 1)[-1] != "Types":
            return
        kind = stack[1].rsplit("}", 1)[-1]
        content_type = attrs.get("ContentType", "")
        if content_type.lower().endswith(("+xml", "/xml")):
            part = attrs.get("PartName", "") if kind == "Override" else "." + attrs.get("Extension", "")
            if not part.lower().endswith(_XML_SUFFIXES):
                raise InputError("Workbook declares XML in an unsupported ZIP part.")
        if kind == "Override" and content_type == _WORKBOOK_TYPE:
            part = attrs.get("PartName", "").removeprefix("/")
            if part not in names:
                raise InputError("Workbook package is missing its XLSX workbook part.")
            workbooks.append(PurePosixPath(part))

    if "[Content_Types].xml" not in names:
        raise InputError("Expected an XLSX workbook package.")
    _parse_xml(archive, "[Content_Types].xml", inspect_type)
    if len(workbooks) != 1:
        raise InputError("Expected one standard, macro-free XLSX workbook.")
    return workbooks[0]


def _inspect_part(archive: ZipFile, name: str, workbook_rels: str, limits: UploadLimits) -> None:
    extent = _WorksheetExtent(limits)

    def inspect_element(stack: list[str], attrs: dict[str, str]) -> None:
        if stack[-1] == _WORKSHEET_ROW:
            extent.start_row(attrs.get("r"))
        elif len(stack) > 1 and stack[-2] == _WORKSHEET_ROW:
            extent.add_cell(attrs.get("r"))
        elif len(stack) == 2 and stack[-1].rsplit("}", 1)[-1] == "Relationship":
            if name != workbook_rels and attrs.get("TargetMode") == "External":
                return
            relation = attrs.get("Type", "").rsplit("/", 1)[-1]
            if name == workbook_rels or relation not in _BINARY_RELATIONSHIPS:
                target = attrs.get("Target", "")
                if not target.lower().endswith(_XML_SUFFIXES):
                    raise InputError("Workbook links to an unsupported internal ZIP part.")

    _parse_xml(archive, name, inspect_element)


def _inspect_xlsx(content: bytes, limits: UploadLimits) -> None:
    with ZipFile(BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > limits.max_zip_entries:
            raise InputError(f"Workbook exceeds the {limits.max_zip_entries:,}-ZIP-entry limit.")
        if sum(entry.file_size for entry in entries) > limits.max_expanded_bytes:
            raise InputError(f"Workbook exceeds the expanded size limit of {limits.max_expanded_bytes / _MIB:g} MiB.")
        if len({entry.filename for entry in entries}) != len(entries):
            raise InputError("Workbook ZIP contains duplicate part names.")
        for entry in entries:
            if entry.flag_bits & 1 or entry.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
                raise InputError("Workbook ZIP is encrypted or uses unsupported compression.")
            if entry.file_size and not entry.compress_size:
                raise InputError(f"Workbook ZIP part {entry.filename!r} has an invalid compressed size.")
            # Small XML parts often compress well. Reserve the ratio guard for large parts.
            if entry.file_size >= _MIB and entry.file_size > limits.max_expansion_ratio * entry.compress_size:
                raise InputError(
                    f"Workbook ZIP part {entry.filename!r} exceeds the "
                    f"{limits.max_expansion_ratio:,}:1 expansion ratio limit for parts of at least 1 MiB."
                )
        workbook = _workbook_part(archive)
        workbook_rels = (workbook.parent / "_rels" / (workbook.name + ".rels")).as_posix()
        for entry in entries:
            name = entry.filename
            if name != "[Content_Types].xml" and name.lower().endswith(_XML_SUFFIXES):
                _inspect_part(archive, name, workbook_rels, limits)


def validate_upload(filename: str, content: bytes, limits: UploadLimits) -> str:
    """Validate upload bytes and return the supported file extension."""
    suffix = PurePosixPath(filename).suffix.lower()
    try:
        if suffix not in {".csv", ".xlsx"}:
            raise InputError("Choose a .csv or .xlsx file.")
        if not content:
            raise InputError("The file is empty.")
        if len(content) > limits.max_bytes:
            raise InputError(f"File exceeds the upload size limit of {limits.max_bytes / _MIB:g} MiB.")
        if suffix == ".csv":
            if _CSV_CONTROL_BYTES.search(content):
                raise InputError("Expected a UTF-8 CSV file without binary or NUL bytes.")
        else:
            _inspect_xlsx(content, limits)
    except (BadZipFile, OSError, RuntimeError, ValueError, LookupError, DecompressionError, expat.ExpatError) as error:
        raise InputError(f"{filename}: {error}") from error
    return suffix


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    """Keep checked bytes with the limits used to check them."""

    filename: str
    content: bytes = field(repr=False)
    limits: UploadLimits
    suffix: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.content) is not bytes:
            raise TypeError("Validated upload content must be immutable bytes.")
        object.__setattr__(self, "suffix", validate_upload(self.filename, self.content, self.limits))
