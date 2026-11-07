from dataclasses import asdict
from decimal import Decimal, localcontext

import pandas as pd
import pytest

from reconcileiq import ColumnMapping, ConfigurationError, ReconciliationConfig, reconcile


MAPPING = ColumnMapping(("id",), "amount", "status", "currency")


def _frame(rows):
    return pd.DataFrame(rows, columns=MAPPING.required_columns, dtype=object)


@pytest.mark.parametrize(
    "a_amount,b_amount,delta,classification,flags",
    [
        ("10", "10.000", "0", "matched", ()),
        ("10.009999999999999999", "10", "0.009999999999999999", "matched_within_tolerance", ("amount_within_tolerance",)),
        ("-10.01", "-10", "-0.01", "matched_within_tolerance", ("amount_within_tolerance",)),
        ("-10", "-10.01", "0.01", "matched_within_tolerance", ("amount_within_tolerance",)),
        ("-10.010000000000000001", "-10", "-0.010000000000000001", "amount_mismatch", ("amount_mismatch",)),
    ],
)
def test_tolerance_is_inclusive_and_signed_delta_remains_exact(a_amount, b_amount, delta, classification, flags):
    config = ReconciliationConfig(MAPPING, MAPPING, amount_tolerance=Decimal("0.01"))
    result = reconcile(_frame([("key", a_amount, "PAID", "USD")]), _frame([("key", b_amount, "PAID", "USD")]), config)
    record, = result.records

    assert record.amount_delta == Decimal(delta)
    assert record.classification == classification
    assert record.flags == flags
    assert result.summary["totals_by_currency"]["USD"]["delta"] == Decimal(delta)


def test_tolerance_changes_only_valid_comparable_amount_outcomes_and_preserves_match_rate_grain():
    a = _frame([
        ("exact", "100.00", "PAID", "USD"),
        ("tolerated", "10.00", "PAID", "USD"),
        ("status", "10.00", "PAID", "USD"),
        ("invalid", "10.00", "", "USD"),
        ("currency", "10.00", "PAID", "USD"),
        ("unmatched", "12.00", "PAID", "EUR"),
    ])
    b = _frame([
        ("exact", "100", "PAID", "USD"),
        ("tolerated", "10.01", "PAID", "USD"),
        ("status", "10.01", "PENDING", "USD"),
        ("invalid", "10.01", "PAID", "USD"),
        ("currency", "10.01", "PAID", "EUR"),
    ])
    config = ReconciliationConfig(MAPPING, MAPPING, amount_tolerance=Decimal("0.01"))
    result = reconcile(a, b, config)
    records = {record.key[0]: record for record in result.records}
    summary = result.summary

    assert records["status"].classification == "status_mismatch"
    assert records["status"].flags == ("amount_within_tolerance", "status_mismatch")
    assert records["invalid"].classification == "invalid_value"
    assert records["invalid"].flags == ("amount_within_tolerance", "invalid_status")
    assert records["currency"].flags == ("currency_mismatch",)
    assert records["currency"].amount_delta is None
    assert records["unmatched"].flags == ("missing_in_source_b",)
    assert records["unmatched"].amount_delta is None
    assert summary["eligible_records"] == 6
    assert summary["exact_matched_records"] == summary["tolerated_matched_records"] == 1
    assert summary["matched_records"] == 2
    assert summary["match_rate"] == pytest.approx(2 / 6)
    assert summary["flag_counts"]["amount_within_tolerance"] == 3
    assert summary["totals_by_currency"] == reconcile(a, b, ReconciliationConfig(MAPPING, MAPPING)).summary["totals_by_currency"]


def test_tolerance_preserves_smallest_supported_delta_at_large_magnitude():
    a = _frame([("high", "99999999999999999999999999999999999999.123456789012345678", "PAID", "GBP")])
    b = _frame([("high", "99999999999999999999999999999999999999.123456789012345677", "PAID", "GBP")])
    tolerance = Decimal("0.000000000000000001")
    config = ReconciliationConfig(MAPPING, MAPPING, amount_tolerance=tolerance)

    with localcontext() as context:
        context.prec = 6
        record, = reconcile(a, b, config).records

    assert record.classification == "matched_within_tolerance"
    assert record.amount_delta == tolerance


def test_tolerance_configuration_is_optional_and_exact():
    data = {"source_a": asdict(MAPPING), "source_b": asdict(MAPPING)}
    default = ReconciliationConfig.from_dict(data)
    zero = ReconciliationConfig.from_dict({**data, "amount_tolerance": "0.00"})
    exact = ReconciliationConfig.from_dict({**data, "amount_tolerance": "0.123456789012345678"})

    assert default == zero
    assert default.amount_tolerance == Decimal(0)
    assert exact.amount_tolerance == Decimal("0.123456789012345678")
    a = _frame([("key", "1.00", "PAID", "USD")])
    b = _frame([("key", "1.01", "PAID", "USD")])
    assert reconcile(a, b, default) == reconcile(a, b, zero)
    assert reconcile(a, b, zero).records[0].classification == "amount_mismatch"


@pytest.mark.parametrize(
    "value", ["-0.01", "", None, False, 0.01, "1e-3", "0.0000000000000000001", Decimal("NaN"), Decimal("Infinity")],
)
def test_tolerance_rejects_invalid_or_inexact_settings(value):
    data = {"source_a": asdict(MAPPING), "source_b": asdict(MAPPING), "amount_tolerance": value}
    with pytest.raises(ConfigurationError, match="amount_tolerance must be a nonnegative decimal"):
        ReconciliationConfig.from_dict(data)
