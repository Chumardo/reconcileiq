"""Command-line entry point for file-based reconciliation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import ConfigurationError, ReconciliationConfig
from .engine import SchemaError, reconcile
from .export import OutputError, write_outputs
from .inputs import InputError, load_source


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f"Duplicate configuration property {key!r}.")
        result[key] = value
    return result


def _load_config(path: Path) -> ReconciliationConfig:
    try:
        with path.open(encoding="utf-8-sig") as stream:
            data = json.load(stream, object_pairs_hook=_unique_object)
        return ReconciliationConfig.from_dict(data)
    except (OSError, UnicodeError, json.JSONDecodeError, ConfigurationError) as error:
        raise ConfigurationError(f"{path}: {error}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reconcileiq", description="Reconcile two transaction datasets and audit data quality."
    )
    parser.add_argument("--source-a", type=Path, required=True, help="CSV or XLSX for source A")
    parser.add_argument("--source-b", type=Path, required=True, help="CSV or XLSX for source B")
    parser.add_argument("--config", type=Path, required=True, help="JSON column mappings and key types")
    parser.add_argument("--output-dir", type=Path, required=True, help="Destination for JSON and CSV")
    parser.add_argument("--sheet-a", help="Worksheet name for source A XLSX")
    parser.add_argument("--sheet-b", help="Worksheet name for source B XLSX")
    args = parser.parse_args(argv)

    try:
        config = _load_config(args.config)
        source_a, rows_a = load_source(
            args.source_a, sheet=args.sheet_a, amount_column=config.source_a.amount,
            integer_key_columns=config.source_a.integer_key_columns(config.key_types),
        )
        source_b, rows_b = load_source(
            args.source_b, sheet=args.sheet_b, amount_column=config.source_b.amount,
            integer_key_columns=config.source_b.integer_key_columns(config.key_types),
        )
        result = reconcile(source_a, source_b, config, row_numbers_a=rows_a, row_numbers_b=rows_b)
        write_outputs(
            result, args.output_dir, protected_paths=(args.source_a, args.source_b, args.config)
        )
    except (ConfigurationError, SchemaError, InputError, OutputError, OSError) as error:
        parser.exit(2, f"reconcileiq: error: {error}\n")

    print(
        f"Reconciled {len(source_a):,} source A rows and {len(source_b):,} source B rows "
        f"into {len(result.records):,} records."
    )
    print(f"Wrote report.json and records.csv to {args.output_dir}.")
    return 0
