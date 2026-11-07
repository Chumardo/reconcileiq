"""Create reproducible, entirely synthetic ledger and settlement exports."""

import argparse
import csv
import json
import random
from decimal import Decimal
from pathlib import Path


SEED = 1729
BASELINE_PAIRS = 10_000
DEMO_DIR = Path(__file__).resolve().parents[1] / "data" / "demo"
CONFIG = {
    "source_a": {
        "keys": ["merchant_id", "transaction_id"],
        "amount": "booked_amount",
        "status": "ledger_status",
        "currency": "currency",
        "row_id": "ledger_row",
    },
    "source_b": {
        "keys": ["merchant_ref", "payment_ref"],
        "amount": "settled_amount",
        "status": "bank_status",
        "currency": "ccy",
        "row_id": "bank_row",
    },
}
ROW_ID_FORMATS = {"a": "A-{index:05d}", "b": "B-{index:05d}"}


def baseline_id(side: str, index: int) -> str:
    return ROW_ID_FORMATS[side].format(index=index)


def expected(a: str | None, b: str | None, classification: str, *flags: str) -> dict:
    return {
        "row_id_a": a,
        "row_id_b": b,
        "classification": classification,
        "flags": sorted(flags),
    }


def write_json(path: Path, value: dict) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(value, file, indent=2)
        file.write("\n")


def generate(output_dir: Path) -> None:
    rng = random.Random(SEED)
    sources: dict[str, dict[str, dict[str, str]]] = {"a": {}, "b": {}}
    for index in range(1, BASELINE_PAIRS + 1):
        transaction = {
            "merchant": f"SYNTH-{(index - 1) % 100 + 1:03d}",
            "transaction": f"{(index - 1) // 100 + 1:06d}",
            "amount": f"{Decimal(rng.randint(-50000, 250000)).scaleb(-2):.2f}",
            "status": rng.choice(("PAID", "PENDING", "FAILED")),
            "currency": rng.choice(("USD", "EUR", "GBP")),
        }
        for side, rows in sources.items():
            row_id = baseline_id(side, index)
            rows[row_id] = {"row_id": row_id, **transaction}

    scenarios = []

    def inject(
        name: str,
        mutations: list[tuple[str, str, dict[str, str] | None]],
        expected_records: list[dict],
    ) -> None:
        ledger = []
        for side, row_id, updates in mutations:
            rows = sources[side]
            before = rows.get(row_id)
            if updates is None:
                del rows[row_id]
                after = None
                operation = "delete"
            elif before is None:
                after = rows[row_id] = {**updates, "row_id": row_id}
                operation = "insert"
            else:
                after = rows[row_id] = {**before, **updates}
                operation = "update"
            ledger.append(
                {"source": side, "operation": operation, "row_id": row_id, "before": before, "after": after}
            )
        scenarios.append({"name": name, "mutations": ledger, "expected_records": expected_records})

    inject(
        "Missing in the ledger",
        [("a", "A-00001", None)],
        [expected(None, "B-00001", "missing_in_source_a", "missing_in_source_a")],
    )
    inject(
        "Missing in the settlement export",
        [("b", "B-00002", None)],
        [expected("A-00002", None, "missing_in_source_b", "missing_in_source_b")],
    )
    amount = Decimal(sources["a"]["A-00003"]["amount"])
    inject(
        "Ledger amount differs by one cent",
        [("a", "A-00003", {"amount": str(amount + Decimal("0.01"))})],
        [expected("A-00003", "B-00003", "amount_mismatch", "amount_mismatch")],
    )
    inject(
        "Settlement status differs",
        [("b", "B-00004", {"status": "REVERSED"})],
        [expected("A-00004", "B-00004", "status_mismatch", "status_mismatch")],
    )
    amount = Decimal(sources["b"]["B-00005"]["amount"])
    inject(
        "Both amount and status differ",
        [("b", "B-00005", {"amount": str(amount + Decimal("0.02")), "status": "REVERSED"})],
        [expected("A-00005", "B-00005", "amount_mismatch", "amount_mismatch", "status_mismatch")],
    )
    inject(
        "Duplicate ledger key also quarantines the singleton settlement row",
        [("a", "A-D0006", sources["a"]["A-00006"])],
        [
            expected("A-00006", None, "duplicate_key", "duplicate_key"),
            expected("A-D0006", None, "duplicate_key", "duplicate_key"),
            expected(None, "B-00006", "duplicate_key", "duplicate_key"),
        ],
    )
    inject(
        "Two duplicates on each side require four standalone results",
        [
            ("a", "A-D0007", sources["a"]["A-00007"]),
            ("b", "B-D0007", sources["b"]["B-00007"]),
        ],
        [
            expected("A-00007", None, "duplicate_key", "duplicate_key"),
            expected("A-D0007", None, "duplicate_key", "duplicate_key"),
            expected(None, "B-00007", "duplicate_key", "duplicate_key"),
            expected(None, "B-D0007", "duplicate_key", "duplicate_key"),
        ],
    )
    inject(
        "Blank ledger merchant component leaves the settlement key unmatched",
        [("a", "A-00008", {"merchant": ""})],
        [
            expected("A-00008", None, "invalid_key", "null_key"),
            expected(None, "B-00008", "missing_in_source_a", "missing_in_source_a"),
        ],
    )
    inject(
        "Blank settlement transaction component leaves the ledger key unmatched",
        [("b", "B-00009", {"transaction": ""})],
        [
            expected("A-00009", None, "missing_in_source_b", "missing_in_source_b"),
            expected(None, "B-00009", "invalid_key", "null_key"),
        ],
    )
    inject(
        "Formatted amount has an unsupported grouping separator",
        [("a", "A-00010", {"amount": "1,234.56"})],
        [expected("A-00010", "B-00010", "invalid_value", "invalid_amount")],
    )
    inject(
        "Blank settlement status",
        [("b", "B-00011", {"status": ""})],
        [expected("A-00011", "B-00011", "invalid_value", "invalid_status")],
    )
    inject(
        "Different currencies prevent an amount comparison",
        [("b", "B-00012", {"currency": "CAD"})],
        [expected("A-00012", "B-00012", "currency_mismatch", "currency_mismatch")],
    )
    inject(
        "Currency value contains an invalid character",
        [("a", "A-00013", {"currency": "US$"})],
        [expected("A-00013", "B-00013", "invalid_value", "invalid_currency")],
    )
    inject(
        "Invalid settlement amount and independent status mismatch",
        [("b", "B-00014", {"amount": "NaN", "status": "REVERSED"})],
        [expected("A-00014", "B-00014", "invalid_value", "invalid_amount", "status_mismatch")],
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    for side, rows in sources.items():
        mapping = CONFIG[f"source_{side}"]
        headers = [mapping["row_id"], *mapping["keys"], mapping["amount"], mapping["status"], mapping["currency"]]
        with (output_dir / f"source_{side}.csv").open("w", encoding="utf-8", newline="") as file:
            writer = csv.writer(file, lineterminator="\n")
            writer.writerow(headers)
            for row in rows.values():
                writer.writerow([row[field] for field in ("row_id", "merchant", "transaction", "amount", "status", "currency")])

    write_json(output_dir / "config.json", CONFIG)
    write_json(
        output_dir / "ground_truth.json",
        {
            "schema_version": 1,
            "description": "All data is synthetic. Each untouched baseline index produces one matched pair.",
            "seed": SEED,
            "baseline_pairs": BASELINE_PAIRS,
            "baseline_row_id_formats": ROW_ID_FORMATS,
            "scenarios": scenarios,
        },
    )
    print(f"Wrote {len(sources['a']):,} ledger rows, {len(sources['b']):,} settlement rows "
          f"and {len(scenarios)} anomaly scenarios to {output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEMO_DIR)
    generate(parser.parse_args().output_dir)


if __name__ == "__main__":
    main()
