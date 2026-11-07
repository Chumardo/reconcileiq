"""Validate source values before any key grouping or financial comparison."""

import re
from decimal import Decimal, InvalidOperation
from numbers import Integral

import pandas as pd


MAX_INTEGER_DIGITS = 38
MAX_FRACTION_DIGITS = 18
INVALID_VALUE_FLAGS = frozenset({"invalid_amount", "invalid_status", "invalid_currency"})
_DECIMAL_TEXT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)")
_INTEGER_TEXT = re.compile(r"[+-]?[0-9]+")
_CURRENCY = re.compile(r"[A-Z]{3}")


def normalize_key(values: tuple, kinds: tuple[str, ...]) -> tuple[tuple[str, ...] | None, set[str]]:
    parts = []
    issues = set()
    for value, kind in zip(values, kinds, strict=True):
        if isinstance(value, str):
            value = value.strip()
            if not value:
                issues.add("null_key")
                continue
        elif pd.api.types.is_scalar(value) and not isinstance(value, Decimal) and pd.isna(value):
            issues.add("null_key")
            continue

        if kind == "text" and isinstance(value, str):
            parts.append(value)
        elif kind == "integer" and isinstance(value, Integral) and not isinstance(value, bool):
            parts.append(str(value))
        elif kind == "integer" and isinstance(value, str) and _INTEGER_TEXT.fullmatch(value):
            digits = value.lstrip("+-").lstrip("0") or "0"
            parts.append(f"-{digits}" if value.startswith("-") and digits != "0" else digits)
        else:
            issues.add("invalid_key")
    return (None if issues else tuple(parts)), issues


def parse_amount(value: object) -> Decimal | None:
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, Integral) and not isinstance(value, bool):
        amount = Decimal(int(value))
    elif isinstance(value, str) and _DECIMAL_TEXT.fullmatch(value.strip()):
        try:
            amount = Decimal(value.strip())
        except InvalidOperation:
            return None
    else:
        return None

    if not amount.is_finite():
        return None
    # Bound both scale and magnitude before doing arithmetic with this value.
    if amount.as_tuple().exponent < -MAX_FRACTION_DIGITS:
        return None
    if amount and amount.adjusted() >= MAX_INTEGER_DIGITS:
        return None
    if not amount and amount.as_tuple().exponent > 0:
        return Decimal(0)
    return amount


def parse_status(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    return None


def parse_currency(value: object) -> str | None:
    if isinstance(value, str):
        code = value.strip()
        if _CURRENCY.fullmatch(code):
            return code
    return None
