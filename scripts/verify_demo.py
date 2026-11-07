"""Verify the demo's exported report using the fixtures and ground-truth manifest."""

import argparse
import csv
import json
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path


DEMO_DIR = Path(__file__).resolve().parents[1] / "data" / "demo"
DEMO_CURRENCIES = {"CAD", "EUR", "GBP", "USD"}


class VerificationError(ValueError):
    """A fixture or exported result differs from the expected result."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def read_source(path: Path, mapping: dict) -> dict:
    with path.open(encoding="utf-8", newline="") as file:
        rows = {}
        for row_number, row in enumerate(csv.DictReader(file), start=2):
            row_id = row[mapping["row_id"]]
            require(row_id not in rows, f"Repeated source row ID {row_id} in {path}")
            rows[row_id] = (
                row_number,
                {
                    "row_id": row_id,
                    "merchant": row[mapping["keys"][0]],
                    "transaction": row[mapping["keys"][1]],
                    "amount": row[mapping["amount"]],
                    "status": row[mapping["status"]],
                    "currency": row[mapping["currency"]],
                },
            )
        return rows


def expected_records(manifest: dict, sources: dict) -> dict:
    require(manifest["schema_version"] == 1, "Unsupported ground-truth schema")
    expected = {}
    affected = {"a": set(), "b": set()}

    for scenario in manifest["scenarios"]:
        for mutation in scenario["mutations"]:
            side, row_id = mutation["source"], mutation["row_id"]
            affected[side].add(row_id)
            actual = sources[side].get(row_id)
            payload = actual[1] if actual else None
            require(payload == mutation["after"], f"Manifest mutation differs from input for {row_id}")

        for record in scenario["expected_records"]:
            row_ids = (record["row_id_a"], record["row_id_b"])
            for side, row_id in zip(("a", "b"), row_ids, strict=True):
                if row_id is not None:
                    affected[side].add(row_id)
            require(row_ids not in expected, f"Duplicate manifest entry for {row_ids}")
            expected[row_ids] = (record["classification"], set(record["flags"]))

    for index in range(1, manifest["baseline_pairs"] + 1):
        row_id_a = manifest["baseline_row_id_formats"]["a"].format(index=index)
        row_id_b = manifest["baseline_row_id_formats"]["b"].format(index=index)
        if row_id_a in affected["a"] or row_id_b in affected["b"]:
            continue

        _, row_a = sources["a"][row_id_a]
        _, row_b = sources["b"][row_id_b]
        require(
            {**row_a, "row_id": row_id_b} == row_b,
            f"Untouched baseline rows differ for {row_id_a} and {row_id_b}",
        )
        expected[row_id_a, row_id_b] = ("matched", set())

    return expected


def source_amount(value: str) -> Decimal | None:
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def report_amount(value: str | None) -> Decimal | None:
    if value is None:
        return None
    require(isinstance(value, str), f"Report amount must be a decimal string, received {value!r}")
    amount = source_amount(value)
    require(amount is not None, f"Report amount must be finite, received {value!r}")
    return amount


def verify_source_row(actual: dict, reference: tuple[int, dict]) -> None:
    row_number, row = reference
    row_id = row["row_id"]
    key = [row["merchant"], row["transaction"]]
    require(actual["row_number"] == row_number, f"Incorrect source row number for {row_id}")
    require(
        actual["raw"] == {"key": key, "amount": row["amount"], "status": row["status"], "currency": row["currency"]},
        f"Raw source values differ for {row_id}",
    )
    expected_amount = source_amount(row["amount"])
    expected_status = row["status"] or None
    expected_currency = row["currency"] if row["currency"] in DEMO_CURRENCIES else None
    require(report_amount(actual["amount"]) == expected_amount, f"Incorrect normalized amount for {row_id}")
    require(actual["status"] == expected_status, f"Incorrect normalized status for {row_id}")
    require(actual["currency"] == expected_currency, f"Incorrect normalized currency for {row_id}")
    issues = {
        "null_key": "" in key,
        "invalid_amount": expected_amount is None,
        "invalid_status": expected_status is None,
        "invalid_currency": expected_currency is None,
    }
    expected_issues = sorted(issue for issue, applies in issues.items() if applies)
    require(actual["issues"] == expected_issues, f"Incorrect source-level issues for {row_id}")


def verify_details(records: list, expected: dict, sources: dict) -> None:
    seen_records = set()
    seen_rows = {"a": set(), "b": set()}

    for record in records:
        source_a, source_b = record["source_a"], record["source_b"]
        row_ids = (
            source_a["row_id"] if source_a is not None else None,
            source_b["row_id"] if source_b is not None else None,
        )
        require(row_ids in expected, f"Unexpected pairing or standalone result for {row_ids}")
        require(row_ids not in seen_records, f"Repeated result for {row_ids}")
        seen_records.add(row_ids)
        classification, flags = expected[row_ids]
        require(record["classification"] == classification, f"Incorrect classification for {row_ids}")
        require(record["flags"] == sorted(flags), f"Incorrect issue flags for {row_ids}")

        for side, source, row_id in zip(("a", "b"), (source_a, source_b), row_ids, strict=True):
            if row_id is None:
                continue
            require(row_id in sources[side], f"Unrecognized source row ID {row_id}")
            require(row_id not in seen_rows[side], f"Source row {row_id} used more than once")
            seen_rows[side].add(row_id)
            verify_source_row(source, sources[side][row_id])

        expected_key = None
        if classification != "invalid_key":
            side, row_id = ("a", row_ids[0]) if source_a is not None else ("b", row_ids[1])
            _, row = sources[side][row_id]
            expected_key = [row["merchant"], row["transaction"]]
        require(record["key"] == expected_key, f"Incorrect reconciliation key for {row_ids}")

        expected_delta = None
        if source_a is not None and source_b is not None:
            _, row_a = sources["a"][row_ids[0]]
            _, row_b = sources["b"][row_ids[1]]
            amount_a, amount_b = source_amount(row_a["amount"]), source_amount(row_b["amount"])
            if (
                row_a["currency"] == row_b["currency"]
                and row_a["currency"] in DEMO_CURRENCIES
                and amount_a is not None
                and amount_b is not None
            ):
                expected_delta = amount_a - amount_b
        require(report_amount(record["amount_delta"]) == expected_delta, f"Incorrect amount delta for {row_ids}")

    require(seen_records == set(expected), f"Missing expected records {set(expected) - seen_records}")
    for side in ("a", "b"):
        require(seen_rows[side] == set(sources[side]), f"Source {side.upper()} row conservation failed")


def verify_summary(summary: dict, expected: dict, sources: dict) -> None:
    classifications = Counter(classification for classification, _ in expected.values())
    flags = Counter(flag for _, record_flags in expected.values() for flag in record_flags)
    require(Counter(summary["classification_counts"]) == classifications, "Classification counts differ from manifest")
    require(Counter(summary["flag_counts"]) == flags, "Issue flag counts differ from manifest")
    require(summary["result_records"] == len(expected), "Result record count differs from manifest")

    eligible = len(expected) - classifications["duplicate_key"] - classifications["invalid_key"]
    require(summary["eligible_records"] == eligible, "Match-rate denominator differs from manifest")
    require(summary["matched_records"] == classifications["matched"], "Matched count differs from manifest")
    require(summary["match_rate"] == classifications["matched"] / eligible, "Match rate differs from manifest")

    totals = {"a": defaultdict(Decimal), "b": defaultdict(Decimal)}
    included = {"a": Counter(), "b": Counter()}
    for side, rows in sources.items():
        excluded = 0
        for _, row in rows.values():
            amount = source_amount(row["amount"])
            currency = row["currency"]
            if currency not in DEMO_CURRENCIES or amount is None:
                excluded += 1
            else:
                totals[side][currency] += amount
                included[side][currency] += 1

        require(summary[f"source_rows_{side}"] == len(rows), f"Source {side.upper()} row count differs from CSV")
        require(summary[f"amounts_excluded_{side}"] == excluded, f"Source {side.upper()} excluded amount count differs")

    actual_totals = summary["totals_by_currency"]
    require(set(actual_totals) == set(totals["a"]) | set(totals["b"]), "Reported currency groups differ from sources")
    for currency, actual in actual_totals.items():
        total_a, total_b = totals["a"][currency], totals["b"][currency]
        require(report_amount(actual["source_a"]) == total_a, f"Source A total differs for {currency}")
        require(report_amount(actual["source_b"]) == total_b, f"Source B total differs for {currency}")
        require(report_amount(actual["delta"]) == total_a - total_b, f"Source total delta differs for {currency}")
        require(actual["rows_a"] == included["a"][currency], f"Source A included amount count differs for {currency}")
        require(actual["rows_b"] == included["b"][currency], f"Source B included amount count differs for {currency}")


def verify(demo_dir: Path, report_path: Path) -> None:
    config = read_json(demo_dir / "config.json")
    sources = {
        side: read_source(demo_dir / f"source_{side}.csv", config[f"source_{side}"])
        for side in ("a", "b")
    }
    manifest = read_json(demo_dir / "ground_truth.json")
    report = read_json(report_path)
    expected = expected_records(manifest, sources)
    verify_details(report["records"], expected, sources)
    verify_summary(report["summary"], expected, sources)
    print(
        f"Verified {len(expected):,} result records, all {sum(map(len, sources.values())):,} source rows, "
        f"{len(manifest['scenarios'])} anomaly scenarios, match rate, and exact currency totals."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-dir", type=Path, default=DEMO_DIR)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        verify(args.demo_dir, args.report)
    except VerificationError as exc:
        parser.exit(1, f"Verification failed: {exc}\n")


if __name__ == "__main__":
    main()
