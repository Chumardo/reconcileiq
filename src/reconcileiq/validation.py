"""Validate source values before any key grouping or financial comparison."""

import re
from decimal import Decimal, InvalidOperation
from numbers import Integral

import pandas as pd

from .models import XlsxNumericKey


MAX_INTEGER_DIGITS = 38
MAX_FRACTION_DIGITS = 18
MAX_XLSX_KEY_DIGITS = 309
INVALID_VALUE_FLAGS = frozenset({"invalid_amount", "invalid_status", "invalid_currency"})
_DECIMAL_TEXT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)")
_INTEGER_TEXT = re.compile(r"[+-]?[0-9]+")
_CURRENCY = re.compile(r"[A-Z]{3}")


def _xlsx_integer_key(value: XlsxNumericKey) -> str | None:
    try:
        number = Decimal(value.value)
    except InvalidOperation:
        return None
    if not number.is_finite() or number.adjusted() >= MAX_XLSX_KEY_DIGITS:
        return None
    integer = number.to_integral_value()
    if number != integer:
        return None
    return format(integer, "f") if integer else "0"


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
        elif kind == "integer" and isinstance(value, XlsxNumericKey):
            normalized = _xlsx_integer_key(value)
            if normalized is None:
                issues.add("invalid_key")
            else:
                parts.append(normalized)
        elif kind == "integer" and isinstance(value, Integral) and not isinstance(value, bool):
            parts.append(str(value))
        elif kind == "integer" and isinstance(value, str) and _INTEGER_TEXT.fullmatch(value):
            digits = value.lstrip("+-").lstrip("0") or "0"
            parts.append(f"-{digits}" if value.startswith("-") and digits != "0" else digits)
        else:
            issues.add("invalid_key")
    return (None if issues else tuple(parts)), issues


def parse_amount(value: object) -> Decimal | None:
    # A workbook column can be explicitly mapped as both an integer key and an amount.
    if isinstance(value, XlsxNumericKey):
        try:
            value = Decimal(value.value)
        except InvalidOperation:
            return None
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
