"""The row-level report contract used by the API and exporters."""

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class XlsxNumericKey:
    """Original XLSX numeric token for an explicitly mapped integer key."""

    value: str
    kind: str = field(default="xlsx_number", init=False)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SourceRow:
    row_number: int
    row_id: object | None
    raw: dict[str, object]
    amount: Decimal | None
    status: str | None
    currency: str | None
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReconciliationRecord:
    key: tuple[str, ...] | None
    classification: str
    flags: tuple[str, ...]
    source_a: SourceRow | None
    source_b: SourceRow | None
    amount_delta: Decimal | None


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    records: tuple[ReconciliationRecord, ...]
    summary: dict[str, object]
