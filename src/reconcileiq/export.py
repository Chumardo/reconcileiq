"""Serialize reconciliation results without converting Decimal values to floats."""

from __future__ import annotations

import csv
from dataclasses import fields, is_dataclass
from datetime import date, time, timedelta
from decimal import Decimal
import json
import math
from pathlib import Path
from shutil import copy2
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from .engine import ReconciliationResult

_SOURCE_FIELDS = ("row_number", "row_id", "amount", "status", "currency", "issues", "raw")
_CSV_FIELDS = (
    "key",
    "classification",
    "flags",
    "amount_delta",
    *(f"{side}_{name}" for side in ("source_a", "source_b") for name in _SOURCE_FIELDS),
)
_CSV_USER_TEXT = {f"{side}_{field}" for side in ("source_a", "source_b") for field in ("row_id", "status")}
_FORMULA_START = frozenset("=+-@＝＋－＠")


class OutputError(ValueError):
    """Report destinations conflict with an input or output path."""


def _json_value(value):
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if type(value).__module__ == "numpy" and getattr(value, "ndim", None) == 0:
        item = value.item()
        if type(item) is not type(value):
            return _json_value(item)
    return repr(value)


def _compact_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _spreadsheet_safe_text(value):
    if isinstance(value, str) and value:
        first = next((char for char in value if not char.isspace() and char != "\ufeff"), "")
        if value[0] in "\t\r\n" or first in _FORMULA_START:
            return "'" + value
    return value


def _csv_record(record: dict) -> dict:
    row = {key: record[key] for key in ("key", "classification", "flags", "amount_delta")}
    for side in ("source_a", "source_b"):
        source = record[side] or {}
        row.update({f"{side}_{name}": source.get(name) for name in _SOURCE_FIELDS})
    for field in _CSV_USER_TEXT:
        row[field] = _spreadsheet_safe_text(row[field])
    return {
        key: _compact_json(value) if isinstance(value, (list, dict)) else value
        for key, value in row.items()
    }


def _publish(report_tmp: Path, records_tmp: Path, report_path: Path, records_path: Path) -> None:
    lock = report_path.parent / ".reconcileiq-publish"
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise OutputError(
            f"An output publish lock exists at {lock}. "
            "If all publishers have exited, remove the lock directory and rerun."
        ) from error

    try:
        previous_records = records_tmp.with_name("previous-records.csv")
        had_records = records_path.exists() or records_path.is_symlink()
        if had_records:
            copy2(records_path, previous_records, follow_symlinks=False)
        records_tmp.replace(records_path)
        try:
            report_tmp.replace(report_path)
        except OSError:
            if had_records:
                previous_records.replace(records_path)
            else:
                records_path.unlink()
            raise
    finally:
        lock.rmdir()


def write_outputs(
    result: ReconciliationResult,
    output_dir: str | Path,
    *,
    protected_paths: tuple[Path, ...] = (),
) -> None:
    """Write a JSON report and flat CSV, staging both before replacing files."""
    output_dir = Path(output_dir)
    report_path = output_dir / "report.json"
    records_path = output_dir / "records.csv"
    protected = {path.resolve() for path in protected_paths}
    for path in (report_path, records_path):
        if path.resolve() in protected:
            raise OutputError(f"Output would replace an input file: {path}")
        if path.is_dir():
            raise OutputError(f"Expected a file destination, found a directory: {path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output_dir) as staging:
        report_tmp = Path(staging) / report_path.name
        records_tmp = Path(staging) / records_path.name
        with (
            report_tmp.open("w", encoding="utf-8", newline="\n") as report,
            records_tmp.open("w", encoding="utf-8", newline="") as records,
        ):
            writer = csv.DictWriter(records, fieldnames=_CSV_FIELDS)
            writer.writeheader()
            summary = json.dumps(_json_value(result.summary), ensure_ascii=False, allow_nan=False, indent=2)
            report.write('{\n  "summary": ' + summary.replace("\n", "\n  ") + ',\n  "records": [')
            for index, record in enumerate(result.records):
                data = _json_value(record)
                separator = ",\n" if index else "\n"
                report.write(separator + "    " + _compact_json(data))
                writer.writerow(_csv_record(data))
            report.write("\n  ]\n}\n")
        _publish(report_tmp, records_tmp, report_path, records_path)
