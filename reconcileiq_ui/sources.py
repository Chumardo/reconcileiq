"""Adapt uploaded bytes and bundled fixtures to the shared file loader."""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile

import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from reconcileiq import ConfigurationError, ReconciliationConfig, ReconciliationResult, reconcile
from reconcileiq.inputs import InputError, load_validated_source
from reconcileiq.preflight import UploadLimits, ValidatedUpload


DEMO_DIRECTORY = Path(__file__).resolve().parents[1] / "data" / "demo"
_DEMO_LIMITS = UploadLimits(max_bytes=1024**2, max_rows=12_000, max_columns=8)


@dataclass(frozen=True, slots=True)
class AuditRun:
    result: ReconciliationResult
    source_a: str
    source_b: str
    config: ReconciliationConfig
    is_demo: bool = False
    sheet_a: str | None = None
    sheet_b: str | None = None


def list_worksheets(filename: str, content: bytes, *, limits: UploadLimits = UploadLimits()) -> tuple[str, ...]:
    """Return XLSX worksheet names in workbook order, or an empty tuple for CSV."""
    return list_validated_worksheets(ValidatedUpload(filename, content, limits))


def list_validated_worksheets(upload: ValidatedUpload) -> tuple[str, ...]:
    if upload.suffix == ".csv":
        return ()
    try:
        with BytesIO(upload.content) as stream:
            workbook = load_workbook(stream, read_only=True, keep_links=False)
            try:
                sheets = tuple(sheet.title for sheet in workbook.worksheets)
            finally:
                workbook.close()
    except (OSError, ValueError, LookupError, BadZipFile, SyntaxError, InvalidFileException, TypeError, AttributeError) as error:
        raise InputError(f"{upload.filename}: could not open the workbook. {error}") from error
    if not sheets:
        raise InputError(f"{upload.filename}: the workbook has no worksheets.")
    return sheets


def parse_upload(
    filename: str,
    content: bytes,
    *,
    sheet: str | None = None,
    amount_column: str | None = None,
    integer_key_columns: Collection[str] = (),
    limits: UploadLimits = UploadLimits(),
) -> tuple[pd.DataFrame, tuple[int, ...]]:
    """Read an upload with the same validation and row locators as the CLI."""
    return load_validated_source(
        ValidatedUpload(filename, content, limits), sheet=sheet, amount_column=amount_column,
        integer_key_columns=integer_key_columns,
    )


def validate_mapping(
    data: dict, columns_a: Collection[str], columns_b: Collection[str]
) -> ReconciliationConfig:
    """Build the engine configuration and check it against the loaded headers."""
    config = ReconciliationConfig.from_dict(data)
    for side, mapping, columns in (
        ("A", config.source_a, columns_a),
        ("B", config.source_b, columns_b),
    ):
        missing = [name for name in mapping.required_columns if name not in columns]
        if missing:
            raise ConfigurationError(f"Source {side} is missing mapped columns: {', '.join(missing)}.")
    return config


def _read_demo_source(path: Path) -> tuple[pd.DataFrame, tuple[int, ...]]:
    with path.open("rb") as stream:
        content = stream.read(_DEMO_LIMITS.max_bytes + 1)
    return parse_upload(path.name, content, limits=_DEMO_LIMITS)


def run_demo() -> AuditRun:
    """Reconcile the original synthetic data using its bundled mapping."""
    path_a = DEMO_DIRECTORY / "source_a.csv"
    path_b = DEMO_DIRECTORY / "source_b.csv"
    config_data = json.loads((DEMO_DIRECTORY / "config.json").read_text(encoding="utf-8"))
    frame_a, locators_a = _read_demo_source(path_a)
    frame_b, locators_b = _read_demo_source(path_b)
    config = validate_mapping(config_data, frame_a.columns, frame_b.columns)
    result = reconcile(frame_a, frame_b, config, row_numbers_a=locators_a, row_numbers_b=locators_b)
    return AuditRun(result, path_a.name, path_b.name, config, is_demo=True)
