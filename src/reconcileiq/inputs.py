"""Read transaction files without type inference or loss of row locations."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from io import BytesIO, TextIOWrapper
from pathlib import Path
from typing import BinaryIO
from zipfile import BadZipFile

import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils.cell import coordinate_to_tuple
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.xml.functions import iterparse


class InputError(ValueError):
    """A transaction file could not be loaded as a table."""


@dataclass(frozen=True, slots=True)
class UnsupportedCell:
    """The raw evidence for an XLSX formula or error cell."""

    kind: str
    value: str


def _validate_headers(headers: tuple[object, ...]) -> None:
    if not headers:
        raise InputError("Expected column headers in the first row.")
    seen = set()
    for column, header in enumerate(headers, start=1):
        if not isinstance(header, str) or not header.strip():
            raise InputError(f"Expected a nonempty text header in column {column}.")
        if header in seen:
            raise InputError(f"Duplicate column header {header!r}.")
        seen.add(header)


def _load_csv(
    file: BinaryIO, *, max_rows: int | None, max_columns: int | None, max_cells: int | None
) -> tuple[pd.DataFrame, tuple[int, ...]]:
    rows = []
    locations = []
    with TextIOWrapper(file, encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream, strict=True)
        line_number = 1
        try:
            headers = tuple(next(reader, ()))
            _validate_headers(headers)
            width = len(headers)
            if max_columns is not None and width > max_columns:
                raise InputError(f"CSV exceeds the {max_columns:,}-column limit.")
            while True:
                line_number = reader.line_num + 1
                values = next(reader, None)
                if values is None:
                    break
                if not values:
                    continue
                if len(values) != width:
                    raise InputError(
                        f"CSV line {line_number} has {len(values)} fields, "
                        f"expected {width}."
                    )
                count = len(rows) + 1
                if max_rows is not None and count > max_rows:
                    raise InputError(f"CSV exceeds the {max_rows:,}-data-row limit.")
                if max_cells is not None and count * width > max_cells:
                    raise InputError(f"CSV exceeds the {max_cells:,}-data-cell limit.")
                rows.append(values)
                locations.append(line_number)
        except csv.Error as error:
            raise InputError(f"Malformed CSV record at line {line_number}: {error}") from error
    return pd.DataFrame(rows, columns=headers, dtype=object), tuple(locations)


def _cell_value(cell):
    if cell.data_type == "f":
        # Array and data-table formulas may be objects rather than strings.
        formula = cell.value
        if not isinstance(formula, str):
            formula = getattr(formula, "text", None) or type(formula).__name__
        return UnsupportedCell("formula", formula)
    if cell.data_type == "e":
        return UnsupportedCell("error", cell.value)
    return cell.value


def _stored_amounts(worksheet, amount_index: int) -> dict[int, Decimal]:
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    cell_tag, value_tag, row_tag, formula_tag = (namespace + name for name in ("c", "v", "row", "f"))
    amounts = {}
    row_number = 0
    # The read-only worksheet owns relationship resolution and the ZIP handle.
    # Read its XML for the original numeric tokens before openpyxl casts them.
    with worksheet._get_source() as stream:
        for _, element in iterparse(stream):
            if element.tag != row_tag:
                continue
            row_number = int(element.get("r", row_number + 1))
            column = 0
            for cell in element.findall(cell_tag):
                reference = cell.get("r")
                if reference:
                    cell_row, column = coordinate_to_tuple(reference)
                else:
                    cell_row, column = row_number, column + 1
                if column == amount_index + 1 and cell.get("t", "n") == "n":
                    value = cell.findtext(value_tag)
                    if value and cell.find(formula_tag) is None:
                        amounts[cell_row] = Decimal(value)
            element.clear()
    return amounts


def _load_xlsx(
    file: BinaryIO, sheet: str | None, amount_column: str | None
) -> tuple[pd.DataFrame, tuple[int, ...]]:
    workbook = load_workbook(file, read_only=True, data_only=False, keep_links=False)
    try:
        sheets = {worksheet.title: worksheet for worksheet in workbook.worksheets}
        if not sheets:
            raise InputError("The workbook contains no worksheets.")
        if sheet is None:
            sheet = next(iter(sheets))
        if sheet not in sheets:
            raise InputError(f"Worksheet {sheet!r} does not exist. Available: {', '.join(sheets)}.")

        worksheet = sheets[sheet]
        worksheet.reset_dimensions()
        cells = worksheet.iter_rows()
        header_row = tuple(_cell_value(cell) for cell in next(cells, ()))
        width = max((index for index, value in enumerate(header_row, 1) if value is not None), default=0)
        headers = header_row[:width]
        _validate_headers(headers)
        amount_index = headers.index(amount_column) if amount_column in headers else None
        amounts = _stored_amounts(worksheet, amount_index) if amount_index is not None else {}

        rows = []
        locations = []
        for row_number, cell_row in enumerate(cells, start=2):
            values = [_cell_value(cell) for cell in cell_row]
            if all(value is None for value in values):
                continue
            if any(value is not None for value in values[width:]):
                raise InputError(f"Worksheet {sheet!r} row {row_number} has data outside the headers.")
            if amount_index is not None and amount_index < len(values):
                cell = cell_row[amount_index]
                if cell.data_type == "n" and isinstance(cell.value, (float, int)):
                    if cell.row not in amounts:
                        raise InputError(f"Cannot read the stored amount at {sheet!r}!{cell.coordinate}.")
                    values[amount_index] = amounts[cell.row]
            rows.append(values[:width] + [None] * max(0, width - len(values)))
            locations.append(row_number)
        return pd.DataFrame(rows, columns=headers, dtype=object), tuple(locations)
    finally:
        workbook.close()


def load_source(
    path: str | Path, *, sheet: str | None = None, amount_column: str | None = None
) -> tuple[pd.DataFrame, tuple[int, ...]]:
    """Return a source frame and the corresponding 1-based file or worksheet rows.

    Specify the XLSX amount column to parse its native numeric cells directly
    into Decimal from their stored text. Text cells are always read verbatim.
    """
    path = Path(path)
    try:
        with path.open("rb") as file:
            return _load_source_stream(file, path, sheet=sheet, amount_column=amount_column)
    except OSError as error:
        raise InputError(f"{path}: {error}") from error


def load_source_bytes(
    filename: str,
    content: bytes,
    *,
    sheet: str | None = None,
    amount_column: str | None = None,
    max_csv_rows: int | None = None,
    max_csv_columns: int | None = None,
    max_csv_cells: int | None = None,
) -> tuple[pd.DataFrame, tuple[int, ...]]:
    """Read prevalidated upload bytes using the same parser as local files."""
    with BytesIO(content) as file:
        return _load_source_stream(
            file, Path(filename), sheet=sheet, amount_column=amount_column,
            max_csv_rows=max_csv_rows, max_csv_columns=max_csv_columns, max_csv_cells=max_csv_cells,
        )


def _load_source_stream(
    file: BinaryIO,
    path: Path,
    *,
    sheet: str | None,
    amount_column: str | None,
    max_csv_rows: int | None = None,
    max_csv_columns: int | None = None,
    max_csv_cells: int | None = None,
) -> tuple[pd.DataFrame, tuple[int, ...]]:
    try:
        suffix = path.suffix.lower()
        if suffix == ".csv":
            if sheet is not None:
                raise InputError("Sheet selection is only available for XLSX inputs.")
            return _load_csv(file, max_rows=max_csv_rows, max_columns=max_csv_columns, max_cells=max_csv_cells)
        if suffix == ".xlsx":
            return _load_xlsx(file, sheet, amount_column)
        raise InputError("Expected a .csv or .xlsx file.")
    except (
        OSError, ValueError, BadZipFile, SyntaxError, InvalidFileException, InvalidOperation,
        LookupError, TypeError, AttributeError, OverflowError,
    ) as error:
        raise InputError(f"{path}: {error}") from error
