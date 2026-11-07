from collections import Counter
from decimal import Decimal, localcontext

import pandas as pd
import pytest

from reconcileiq import (
    ColumnMapping,
    ConfigurationError,
    ReconciliationConfig,
    SchemaError,
    reconcile,
)


A_MAPPING = ColumnMapping(("transaction",), "amount", "status", "currency", "reference")
B_MAPPING = ColumnMapping(("id",), "net", "state", "ccy", "entry")
CONFIG = ReconciliationConfig(A_MAPPING, B_MAPPING)


def frame(rows, mapping=A_MAPPING):
    return pd.DataFrame(
        rows,
        columns=[*mapping.keys, mapping.amount, mapping.status, mapping.currency, mapping.row_id],
        dtype=object,
    )


def row(key, amount="10.00", status="SETTLED", currency="USD", *, reference=None):
    return key, amount, status, currency, reference if reference is not None else key


def compare(rows_a, rows_b, **kwargs):
    return reconcile(frame(rows_a), frame(rows_b, B_MAPPING), CONFIG, **kwargs)


def test_perfect_matches_preserve_source_ids_and_row_positions():
    result = compare(
        [row("01", reference="ledger-44"), row("02", "-8.75", reference="ledger-62")],
        [row("02", "-8.750", reference="payout-9"), row("01", "10", reference="payout-3")],
        row_numbers_a=[5, 9],
        row_numbers_b=[4, 8],
    )
    records = {record.key: record for record in result.records}

    assert records.keys() == {("01",), ("02",)}
    assert all(record.classification == "matched" for record in records.values())
    assert all(record.flags == () for record in records.values())
    assert all(record.amount_delta == Decimal(0) for record in records.values())
    assert records[("01",)].source_a.row_id == "ledger-44"
    assert records[("01",)].source_a.row_number == 5
    assert records[("01",)].source_b.row_id == "payout-3"
    assert records[("01",)].source_b.row_number == 8
    assert result.summary["matched_records"] == 2
    assert result.summary["match_rate"] == 1


def test_missing_in_each_source_counts_against_match_rate():
    result = compare([row("only-a"), row("shared")], [row("shared"), row("only-b")])
    records = {record.key[0]: record for record in result.records}

    assert records["only-a"].source_b is None
    assert records["only-a"].classification == "missing_in_source_b"
    assert records["only-a"].flags == ("missing_in_source_b",)
    assert records["only-b"].source_a is None
    assert records["only-b"].classification == "missing_in_source_a"
    assert records["only-b"].flags == ("missing_in_source_a",)
    assert records["only-b"].amount_delta is None
    assert result.summary["eligible_records"] == 3
    assert result.summary["match_rate"] == pytest.approx(1 / 3)


@pytest.mark.parametrize("a_count,b_count", [(2, 3), (1, 2), (2, 1), (2, 0)])
def test_duplicate_key_quarantines_every_row_for_the_key(a_count, b_count):
    result = compare(
        [row("duplicate", reference=f"A{i}") for i in range(a_count)],
        [row("duplicate", reference=f"B{i}") for i in range(b_count)],
    )

    assert len(result.records) == a_count + b_count
    assert all(record.key == ("duplicate",) for record in result.records)
    assert all(record.classification == "duplicate_key" for record in result.records)
    assert all(record.flags == ("duplicate_key",) for record in result.records)
    assert all((record.source_a is None) != (record.source_b is None) for record in result.records)
    assert {record.source_a.row_id for record in result.records if record.source_a} == {
        f"A{i}" for i in range(a_count)
    }
    assert {record.source_b.row_id for record in result.records if record.source_b} == {
        f"B{i}" for i in range(b_count)
    }
    assert result.summary["eligible_records"] == 0
    assert result.summary["match_rate"] is None


@pytest.mark.parametrize(
    "key,flag",
    [(None, "null_key"), ("", "null_key"), (" \t", "null_key"), (False, "invalid_key"), (123, "invalid_key")],
)
def test_invalid_and_null_keys_remain_separate_physical_rows(key, flag):
    result = compare([row(key, reference="left")], [row(key, reference="right")])

    assert len(result.records) == 2
    for record in result.records:
        assert record.key is None
        assert record.classification == "invalid_key"
        assert record.flags == (flag,)
        source = record.source_a or record.source_b
        assert source.raw["key"] == (key,)
    assert result.summary["eligible_records"] == 0
    assert result.summary["match_rate"] is None


@pytest.mark.parametrize(
    "b_amount,b_status,classification,flags,delta",
    [
        ("8.25", "SETTLED", "amount_mismatch", ("amount_mismatch",), "1.75"),
        ("10", "FAILED", "status_mismatch", ("status_mismatch",), "0"),
        ("11.33", "FAILED", "amount_mismatch", ("amount_mismatch", "status_mismatch"), "-1.33"),
    ],
)
def test_comparison_flags_and_exclusive_primary_classification(b_amount, b_status, classification, flags, delta):
    result = compare([row("t1")], [row("t1", b_amount, b_status)])
    record, = result.records

    assert record.classification == classification
    assert record.flags == flags
    assert record.amount_delta == Decimal(delta)
    assert result.summary["classification_counts"][classification] == 1
    assert sum(result.summary["classification_counts"].values()) == 1
    assert all(result.summary["flag_counts"][flag] == 1 for flag in flags)


def test_composite_keys_preserve_boundaries_and_leading_zeros():
    a = ColumnMapping(("merchant", "transaction"), "amount", "status", "currency", "reference")
    b = ColumnMapping(("party", "id"), "net", "state", "ccy", "entry")
    config = ReconciliationConfig(a, b)
    common = [("A|B", "C"), ("A", "B|C"), ("A", "01"), ("A", "1"), ("B", "1")]
    a_rows = [(key[0], *row(key[1], str(index))) for index, key in enumerate(common)]
    result = reconcile(frame(a_rows, a), frame(list(reversed(a_rows)), b), config)

    assert {record.key for record in result.records} == set(common)
    assert all(record.classification == "matched" for record in result.records)


def test_invalid_composite_key_keeps_issues_from_each_component():
    a = ColumnMapping(("merchant", "transaction"), "amount", "status", "currency", "reference")
    b = ColumnMapping(("party", "id"), "net", "state", "ccy", "entry")
    config = ReconciliationConfig(a, b)
    result = reconcile(frame([(None, *row(False))], a), frame([], b), config)
    record, = result.records

    assert record.classification == "invalid_key"
    assert record.key is None
    assert record.flags == ("invalid_key", "null_key")


def test_integer_key_type_normalizes_across_input_representations():
    config = ReconciliationConfig(A_MAPPING, B_MAPPING, key_types=("integer",))
    result = reconcile(frame([row("+0042")]), frame([row(42)], B_MAPPING), config)
    record, = result.records

    assert record.key == ("42",)
    assert record.classification == "matched"


def test_decimal_comparison_and_totals_ignore_ambient_precision():
    a = Decimal("99999999999999999999999999999999999999.123456789012345678")
    b = Decimal("99999999999999999999999999999999999999.123456789012345677")
    with localcontext() as context:
        context.prec = 6
        result = compare([row("large", a), row("tiny", "0.1")], [row("large", b), row("tiny", "0.10")])
    records = {record.key[0]: record for record in result.records}

    assert records["large"].classification == "amount_mismatch"
    assert records["large"].amount_delta == Decimal("0.000000000000000001")
    assert records["tiny"].classification == "matched"
    assert result.summary["totals_by_currency"]["USD"] == {
        "source_a": Decimal("99999999999999999999999999999999999999.223456789012345678"),
        "source_b": Decimal("99999999999999999999999999999999999999.223456789012345677"),
        "delta": Decimal("0.000000000000000001"),
        "rows_a": 2,
        "rows_b": 2,
    }


@pytest.mark.parametrize(
    "value",
    [None, "", "1,000.00", "1e-8", "NaN", "Infinity", True, 0.1, "0.0000000000000000001", "1" + "0" * 38],
)
def test_invalid_amount_is_preserved_and_status_still_compared(value):
    result = compare([row("bad", value)], [row("bad", status="FAILED")])
    record, = result.records

    assert record.classification == "invalid_value"
    assert record.flags == ("invalid_amount", "status_mismatch")
    assert record.source_a.raw["amount"] == value
    assert record.source_a.amount is None
    assert record.amount_delta is None
    assert result.summary["amounts_excluded_a"] == 1
    assert result.summary["eligible_records"] == 1


def test_invalid_status_still_allows_an_amount_comparison():
    result = compare([row("bad", status=False)], [row("bad", "11.00")])
    record, = result.records

    assert record.classification == "invalid_value"
    assert record.flags == ("amount_mismatch", "invalid_status")
    assert record.amount_delta == Decimal("-1.00")
    assert result.summary["amounts_excluded_a"] == 0


def test_currency_mismatch_disables_amount_comparison_and_keeps_status_flag():
    result = compare([row("t1")], [row("t1", "9.00", "FAILED", "EUR")])
    record, = result.records

    assert record.classification == "currency_mismatch"
    assert record.flags == ("currency_mismatch", "status_mismatch")
    assert record.amount_delta is None
    assert result.summary["totals_by_currency"]["USD"]["source_a"] == Decimal("10.00")
    assert result.summary["totals_by_currency"]["EUR"]["source_b"] == Decimal("9.00")


def test_invalid_currency_is_flagged_and_excluded_from_totals():
    result = compare([row("bad", currency="EURO")], [row("bad")])
    record, = result.records

    assert record.classification == "invalid_value"
    assert record.flags == ("invalid_currency",)
    assert record.source_a.raw["currency"] == "EURO"
    assert record.amount_delta is None
    assert set(result.summary["totals_by_currency"]) == {"USD"}
    assert result.summary["amounts_excluded_a"] == 1


def test_missing_and_duplicate_rows_keep_invalid_payload_flags():
    result = compare(
        [row("only-a", status=False), row("duplicate", "broken"), row("duplicate")],
        [row("duplicate")],
    )
    issues = {record.flags: record.classification for record in result.records}

    assert issues[("invalid_status", "missing_in_source_b")] == "missing_in_source_b"
    assert issues[("duplicate_key", "invalid_amount")] == "duplicate_key"
    assert result.summary["flag_counts"]["invalid_amount"] == 1
    assert result.summary["flag_counts"]["invalid_status"] == 1


def test_both_empty_datasets_keep_a_zero_summary_and_undefined_match_rate():
    result = compare([], [])

    assert result.records == ()
    assert result.summary["source_rows_a"] == 0
    assert result.summary["source_rows_b"] == 0
    assert result.summary["eligible_records"] == 0
    assert result.summary["totals_by_currency"] == {}
    assert result.summary["match_rate"] is None
    assert set(result.summary["classification_counts"].values()) == {0}


def test_one_empty_dataset_classifies_present_records_as_missing_counterparts():
    result = compare([], [row("only-b")])
    record, = result.records

    assert record.classification == "missing_in_source_a"
    assert result.summary["eligible_records"] == 1
    assert result.summary["match_rate"] == 0


def test_rows_and_totals_are_conserved_across_all_classifications():
    left = [
        row("matched", "0.10"),
        row("amount-diff", "-1.20"),
        row("only-a", "5.05", currency="EUR"),
        row("duplicate", "0.20"),
        row("duplicate", "0.30"),
        row(None, "-2.00"),
        row("invalid-amount", "n/a"),
    ]
    right = [
        row("matched", "0.10"),
        row("amount-diff", "-1.25"),
        row("only-b", "3.05", currency="EUR"),
        row("duplicate", "0.25"),
        row("invalid-amount", "2.00"),
    ]
    a_positions = list(range(10, 10 + len(left)))
    b_positions = list(range(20, 20 + len(right)))
    result = compare(left, right, row_numbers_a=a_positions, row_numbers_b=b_positions)
    summary = result.summary

    assert Counter(r.source_a.row_number for r in result.records if r.source_a) == Counter(a_positions)
    assert Counter(r.source_b.row_number for r in result.records if r.source_b) == Counter(b_positions)
    assert summary["source_rows_a"] == len(left)
    assert summary["source_rows_b"] == len(right)
    assert summary["result_records"] == len(result.records) == 9
    assert sum(summary["classification_counts"].values()) == 9
    assert summary["eligible_records"] == 5
    assert summary["matched_records"] == 1
    assert summary["match_rate"] == pytest.approx(0.2)
    assert summary["totals_by_currency"] == {
        "USD": {"source_a": Decimal("-2.60"), "source_b": Decimal("1.10"), "delta": Decimal("-3.70"),
                "rows_a": 5, "rows_b": 4},
        "EUR": {"source_a": Decimal("5.05"), "source_b": Decimal("3.05"), "delta": Decimal("2.00"),
                "rows_a": 1, "rows_b": 1},
    }
    assert (summary["amounts_excluded_a"], summary["amounts_excluded_b"]) == (1, 0)


def test_required_columns_are_checked_even_on_empty_dataframes():
    bad = frame([]).drop(columns=["amount"])
    with pytest.raises(SchemaError, match="amount"):
        reconcile(bad, frame([], B_MAPPING), CONFIG)


def test_mappings_must_have_the_same_key_arity():
    b = ColumnMapping(("id", "batch"), "net", "state", "ccy", "entry")
    with pytest.raises(ConfigurationError):
        ReconciliationConfig(A_MAPPING, b)
