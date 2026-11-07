"""Typed reconciliation settings shared by the CLI and Python API."""

from dataclasses import dataclass
from decimal import Decimal

from .validation import MAX_FRACTION_DIGITS, MAX_INTEGER_DIGITS, parse_amount


class ConfigurationError(ValueError):
    """The reconciliation configuration is incomplete or inconsistent."""


@dataclass(frozen=True, slots=True)
class ColumnMapping:
    keys: tuple[str, ...]
    amount: str
    status: str
    currency: str
    row_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.keys, (list, tuple)) or not self.keys:
            raise ConfigurationError("keys must be a nonempty array of column names")
        names = (*self.keys, self.amount, self.status, self.currency)
        if self.row_id is not None:
            names += (self.row_id,)
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise ConfigurationError("column names must be nonempty strings")
        if len(set(self.keys)) != len(self.keys):
            raise ConfigurationError("key columns must be distinct within each source")
        object.__setattr__(self, "keys", tuple(self.keys))

    @property
    def required_columns(self) -> tuple[str, ...]:
        columns = (*self.keys, self.amount, self.status, self.currency)
        if self.row_id is not None:
            columns += (self.row_id,)
        return tuple(dict.fromkeys(columns))


@dataclass(frozen=True, slots=True)
class ReconciliationConfig:
    source_a: ColumnMapping
    source_b: ColumnMapping
    key_types: tuple[str, ...] = ()
    amount_tolerance: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if not isinstance(self.source_a, ColumnMapping) or not isinstance(self.source_b, ColumnMapping):
            raise ConfigurationError("source_a and source_b must be column mappings")
        arity = len(self.source_a.keys)
        if arity != len(self.source_b.keys):
            raise ConfigurationError("both sources must map the same number of key components")
        if not isinstance(self.key_types, (list, tuple)):
            raise ConfigurationError("key_types must be an array of key types")
        key_types = tuple(self.key_types) if self.key_types else ("text",) * arity
        if len(key_types) != arity or any(kind not in ("text", "integer") for kind in key_types):
            raise ConfigurationError("provide one 'text' or 'integer' key type per key component")
        object.__setattr__(self, "key_types", key_types)
        tolerance = parse_amount(self.amount_tolerance)
        if tolerance is None or tolerance < 0:
            raise ConfigurationError(
                "amount_tolerance must be a nonnegative decimal. Use plain decimal text for fractions, "
                f"with at most {MAX_INTEGER_DIGITS} integer digits and {MAX_FRACTION_DIGITS} fractional digits."
            )
        object.__setattr__(self, "amount_tolerance", tolerance)

    @classmethod
    def from_dict(cls, data: dict) -> "ReconciliationConfig":
        if not isinstance(data, dict):
            raise ConfigurationError("configuration must be a JSON object")
        unexpected = data.keys() - {"source_a", "source_b", "key_types", "amount_tolerance"}
        if unexpected:
            raise ConfigurationError(f"unknown configuration fields: {', '.join(sorted(unexpected))}")
        mappings = []
        for source in ("source_a", "source_b"):
            mapping = data.get(source)
            if not isinstance(mapping, dict):
                raise ConfigurationError(f"{source} must be a column mapping object")
            try:
                mappings.append(ColumnMapping(**mapping))
            except (TypeError, ConfigurationError) as exc:
                raise ConfigurationError(f"{source}: {exc}") from exc
        return cls(*mappings, key_types=data.get("key_types", ()), amount_tolerance=data.get("amount_tolerance", 0))
