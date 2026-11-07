"""Write a traceable XLSX snapshot of a completed reconciliation."""

from collections import Counter
from collections.abc import Iterable, Sequence
from copy import copy
from decimal import Decimal
from functools import lru_cache
from io import BytesIO
import re

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .audit import currency_audit, dashboard_metrics, issue_impact, quality_by_source, unmatched_amount
from .config import ReconciliationConfig
from .export import _compact_json, _json_value
from .models import ReconciliationRecord, ReconciliationResult, SourceRow


_HEADER_ROW = 5
_EXCEL_ROWS = 1_048_576
_EXCEL_TEXT_UNITS = 32_767
_UNSAFE_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_PRECISION_SOURCE = "https://support.microsoft.com/en-us/excel/change-formula-recalculation-iteration-or-precision-in-excel"
_PRECISION_NOTE = (
    "Excel supports 15 significant digits. Exact decimals that exceed Excel's precision or fail a safe numeric "
    "round trip appear as text in amber. Excel sums skip text cells. The report totals were calculated in Decimal."
)
_MATCH_RATE_NOTE = (
    "Matched records divided by eligible records. Eligible records include missing counterparts, mismatches, "
    "and invalid-value records. Duplicate-key and invalid-key records are excluded."
)

_TITLE = Font(name="Arial", size=17, bold=True, color="15262D")
_NOTE = Font(name="Arial", size=10, italic=True, color="4D6572")
_HEADER = Font(name="Arial", size=10, bold=True, color="FFFFFF")
_BODY = Font(name="Arial", size=10, color="15262D")
_EXACT_TEXT = Font(name="Arial", size=10, color="875618")
_HEADER_FILL = PatternFill("solid", fgColor="24434B")
_STRIPE_FILL = PatternFill("solid", fgColor="F1F6F6")
_HEADER_BORDER = Border(right=Side(style="thin", color="527078"))
_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_LEFT = Alignment(horizontal="left", vertical="center")
_RIGHT = Alignment(horizontal="right", vertical="center")
_WRAPPED = Alignment(horizontal="left", vertical="center", wrap_text=True)
_IMPACT_STYLES = {
    "High": ("9E363E", "FBECEF"),
    "Review": ("805416", "FFF2DF"),
    "Within tolerance": ("086157", "E6F5F1"),
}
_IMPACT_FORMATS = {
    label: (Font(name="Arial", size=10, bold=True, color=font), PatternFill("solid", fgColor=fill))
    for label, (font, fill) in _IMPACT_STYLES.items()
}


class AuditWorkbookError(ValueError):
    """The requested report exceeds Excel's supported dimensions."""


def _excel_text(value: str) -> str:
    value = _UNSAFE_XML.sub(lambda match: f"\\u{ord(match[0]):04x}", value)
    lines = value.split("\n", 254)
    if len(lines) > 254:
        value = "\n".join(lines[:254]) + "\\n" + lines[-1].replace("\n", "\\n")
    # Excel's text capacity is measured in UTF-16 units, including surrogate pairs.
    if len(value) > _EXCEL_TEXT_UNITS // 2:
        encoded = value.encode("utf-16-le")
        if len(encoded) > 2 * _EXCEL_TEXT_UNITS:
            marker = " … [truncated]"
            encoded = encoded[:2 * (_EXCEL_TEXT_UNITS - len(marker))]
            value = encoded.decode("utf-16-le", errors="ignore") + marker
    return value


@lru_cache(maxsize=512)
def _safe_native_number(value: Decimal) -> bool:
    # Excel uses 15 significant digits. Also check openpyxl's 16-digit numeric
    # serialization and the normal Python decimal reconstruction on XLSX read.
    binary = float(value)
    return (
        Decimal(format(binary, ".15g")) == value
        and Decimal(format(binary, ".16g")) == value
        and Decimal(str(binary)) == value
    )


def _styled_cell(sheet, stripe, number_format, alignment, exact_text, impact):
    cell = WriteOnlyCell(sheet)
    cell.font = _BODY
    cell.alignment = {"left": _LEFT, "right": _RIGHT, "wrapped": _WRAPPED}[alignment]
    cell.number_format = number_format
    if stripe:
        cell.fill = _STRIPE_FILL
    if exact_text:
        cell.font = _EXACT_TEXT
    if impact in _IMPACT_FORMATS:
        cell.font, cell.fill = _IMPACT_FORMATS[impact]
    return cell


def _cell(sheet, value, *, styles=None, stripe=False, percent=False, impact=False, wrap=False):
    if value is None:
        return None
    exact_text = False
    impact_style = value if impact else None
    if isinstance(value, Decimal):
        alignment = "right"
        if _safe_native_number(value):
            places = max(2, -value.as_tuple().exponent)
            number_format = "#,##0." + "0" * places
        else:
            value = format(value, "f")
            number_format, exact_text = "@", True
    elif isinstance(value, str):
        value = _excel_text(value)
        alignment, number_format = ("wrapped" if wrap else "left"), "@"
    else:
        alignment, number_format = "right", ("0.00%" if percent else "#,##0")

    style_key = (stripe, number_format, alignment, exact_text, impact_style)
    if styles is None:
        cell = _styled_cell(sheet, *style_key)
    else:
        template = styles.get(style_key)
        if template is None:
            template = styles[style_key] = _styled_cell(sheet, *style_key)
        # The worksheet assigns row and column coordinates while streaming.
        cell = copy(template)
    cell.value = value
    if isinstance(value, str):
        # Assign after value so openpyxl cannot retain an inferred formula or error type.
        cell.data_type = "s"
    return cell


def _table(
    workbook: Workbook,
    name: str,
    note: str,
    headers: Sequence[str],
    widths: Sequence[int],
    rows: Iterable[Sequence],
    *,
    fixed_columns: int = 0,
    wrap_columns: tuple[int, ...] = (),
    detail_height: int = 17,
) -> None:
    sheet = workbook.create_sheet(name)
    sheet.sheet_view.showGridLines = False
    sheet.sheet_view.zoomScale = 90
    sheet.sheet_properties.tabColor = "08766D" if name == "Executive Summary" else "A5B9C0"
    sheet.freeze_panes = f"{get_column_letter(fixed_columns + 1)}{_HEADER_ROW + 1}"
    sheet.sheet_format.defaultRowHeight = detail_height
    sheet.row_dimensions[1].height = 8
    sheet.row_dimensions[2].height = 27
    sheet.row_dimensions[3].height = 18
    sheet.row_dimensions[4].height = 7
    sheet.row_dimensions[_HEADER_ROW].height = 30
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width

    title_cell = _cell(sheet, name)
    title_cell.font = _TITLE
    note_cell = _cell(sheet, note)
    note_cell.font = _NOTE
    sheet.append([None])
    sheet.append([title_cell])
    sheet.append([note_cell])
    sheet.append([None])
    header_cells = []
    for header in headers:
        cell = _cell(sheet, header)
        cell.font, cell.fill, cell.alignment = _HEADER, _HEADER_FILL, _CENTER
        cell.border = _HEADER_BORDER
        header_cells.append(cell)
    sheet.append(header_cells)
    last_row = _HEADER_ROW
    styles = {}
    for last_row, row in enumerate(rows, start=_HEADER_ROW + 1):
        sheet.append([
            _cell(
                sheet, value, styles=styles, stripe=last_row % 2 == 1,
                percent=isinstance(value, float), impact=header == "Impact", wrap=index in wrap_columns,
            )
            for index, (header, value) in enumerate(zip(headers, row, strict=True))
        ])
    sheet.auto_filter.ref = f"A{_HEADER_ROW}:{get_column_letter(len(headers))}{last_row}"


def _summary_rows(result, config, source_a, source_b):
    metrics = dashboard_metrics(result)
    yield "Coverage", "Match rate", metrics["match_rate"], _MATCH_RATE_NOTE
    yield "Coverage", "Matched", metrics["matched_records"], "Exact matches plus matches within tolerance."
    yield "Coverage", "Exact matches", metrics["exact_matched_records"], "Comparable amount difference equals zero and statuses agree."
    yield "Coverage", "Matches within tolerance", metrics["tolerated_matched_records"], "Nonzero amount difference within the configured limit and statuses agree."
    yield "Coverage", "Eligible exceptions", metrics["eligible_exceptions"], "Eligible records without an accepted match."
    yield "Coverage", "Quarantined / excluded", metrics["excluded_records"], "Records with duplicate, invalid, or null keys."
    yield "Coverage", "Match-rate denominator", metrics["eligible_records"], "Count of eligible result records."
    yield "Coverage", "Amount tolerance", config.amount_tolerance, "Maximum absolute difference in each currency's own units."
    yield "Population", "Source A rows", metrics["source_rows_a"], source_a
    yield "Population", "Source B rows", metrics["source_rows_b"], source_b
    yield "Population", "Result records", metrics["result_records"], "Paired and standalone audit records."
    for classification, count in metrics["classification_counts"].items():
        yield "Primary outcome", classification, count, "Each result has one primary outcome."
    for flag, count in metrics["flag_counts"].items():
        yield "Issue flag", flag, count, "A result can carry more than one flag."


_TRACE_FIELDS = (
    ("file", 25), ("worksheet", 19), ("file row", 14), ("row ID", 20), ("raw key", 31),
    ("raw amount", 25), ("raw status", 21), ("raw currency", 18), ("validation flags", 26),
)
_RESULT_FIELDS = (
    ("Result ID", 15), ("Reconciliation key", 31), ("Primary classification", 28), ("Impact", 19),
    ("Issue flags", 34), ("A − B", 25), ("Unmatched amount", 25), ("Amount comparison", 32),
    ("A amount", 25), ("B amount", 25), ("A currency", 13), ("B currency", 13),
    ("A status", 20), ("B status", 20),
    *((f"{side} {field}", width) for side in ("A", "B") for field, width in _TRACE_FIELDS),
)
_DUPLICATE_FIELDS = (
    ("Reconciliation key", 31), ("Rows in A", 15), ("Rows in B", 15), ("Source", 12),
    ("Result ID", 15), ("Amount", 25), ("Currency", 13), ("Status", 20),
    *((label.title(), width) for label, width in _TRACE_FIELDS),
)


def _raw_text(value):
    value = _json_value(value)
    if value is None or isinstance(value, str):
        return value
    return _compact_json(value)


def _trace(row: SourceRow | None, name: str, sheet: str | None) -> tuple:
    if row is None:
        return (None,) * len(_TRACE_FIELDS)
    return (
        name, sheet, row.row_number, _raw_text(row.row_id), _compact_json(_json_value(row.raw["key"])),
        _raw_text(row.raw["amount"]), _raw_text(row.raw["status"]), _raw_text(row.raw["currency"]),
        ", ".join(row.issues),
    )


def _amount_comparison(record: ReconciliationRecord) -> str:
    if record.amount_delta is not None:
        if not record.amount_delta:
            return "Exact amount"
        if "amount_within_tolerance" in record.flags:
            return "Within tolerance"
        return "Outside tolerance"
    if record.classification == "duplicate_key":
        return "Duplicate key quarantine"
    if record.classification == "invalid_key":
        return "Invalid or null key"
    if record.source_a is None or record.source_b is None:
        return "Counterpart missing"
    if "currency_mismatch" in record.flags:
        return "Different currencies"
    return "Invalid amount or currency"


def _detail_row(index, record, source_a, source_b, sheet_a, sheet_b):
    a, b = record.source_a, record.source_b
    return (
        f"R{index:07d}", _compact_json(record.key) if record.key is not None else None,
        record.classification, issue_impact(record), ", ".join(record.flags), record.amount_delta,
        unmatched_amount(record), _amount_comparison(record),
        a.amount if a else None, b.amount if b else None,
        a.currency if a else None, b.currency if b else None,
        a.status if a else None, b.status if b else None,
        *_trace(a, source_a, sheet_a), *_trace(b, source_b, sheet_b),
    )


def _duplicate_rows(records, source_a, source_b, sheet_a, sheet_b):
    duplicates = [(index, record) for index, record in enumerate(records, start=1) if record.classification == "duplicate_key"]
    counts_a = Counter(record.key for _, record in duplicates if record.source_a is not None)
    counts_b = Counter(record.key for _, record in duplicates if record.source_b is not None)
    for index, record in duplicates:
        if record.source_a is not None:
            side, row, name, sheet = "A", record.source_a, source_a, sheet_a
        else:
            side, row, name, sheet = "B", record.source_b, source_b, sheet_b
        yield (
            _compact_json(record.key), counts_a[record.key], counts_b[record.key], side,
            f"R{index:07d}", row.amount, row.currency, row.status, *_trace(row, name, sheet),
        )


_QUALITY_ROWS = (
    ("source_rows", "Physical source rows", "Every parsed row audited by the reconciliation engine."),
    ("duplicate_keys", "Distinct repeated keys", "Repeated keys within the named source."),
    ("duplicate_key_rows", "Rows with repeated keys", "Rows whose key repeats within the named source."),
    ("duplicate_quarantined_rows", "Rows quarantined for duplicate keys", "Includes a unique counterpart on the other side when present."),
    ("invalid_key", "Invalid key type", "At least one key component has the wrong configured type."),
    ("null_key", "Null or blank key", "At least one key component is null or blank."),
    ("invalid_amount", "Invalid amount", "Includes missing amounts and values outside the accepted decimal format."),
    ("invalid_status", "Invalid status", "Status is missing or is an unsupported value type."),
    ("invalid_currency", "Invalid currency", "Currency is missing or fails the three-letter format rule."),
    ("missing_key", "Missing key component", "At least one key component is missing."),
    ("missing_amount", "Missing amount", "The raw amount is null or blank."),
    ("missing_status", "Missing status", "The raw status is null or blank."),
    ("missing_currency", "Missing currency", "The raw currency is null or blank."),
    ("rows_missing_required", "Rows missing any required value", "Counts each affected source row once."),
    ("excluded_rows", "Rows excluded from match rate", "Duplicate-key and invalid-key rows."),
    ("amounts_excluded", "Rows excluded from currency totals", "The row lacks a valid amount or currency."),
)


def _quality_rows(result):
    quality = quality_by_source(result)
    for field, label, rule in _QUALITY_ROWS:
        yield label, quality["source_a"][field], quality["source_b"][field], rule


_CURRENCY_FIELDS = (
    ("Currency", 14), ("Source coverage", 30), ("Source A total", 23), ("Source B total", 23),
    ("Source A − B", 24), ("A currency rows", 16), ("B currency rows", 16),
    ("A valid amount rows", 18), ("B valid amount rows", 18), ("Comparable pairs", 18),
    ("Compared A total", 23), ("Compared B total", 23), ("Compared A − B", 24),
    ("Matched pairs", 18), ("Matched A total", 23), ("Matched B total", 23), ("Matched A − B", 24),
)


def _currency_rows(result):
    for row in currency_audit(result):
        yield (
            row.currency, row.coverage, row.source_a, row.source_b, row.source_delta,
            row.currency_rows_a, row.currency_rows_b, row.amount_rows_a, row.amount_rows_b,
            row.compared_pairs, row.compared_a, row.compared_b, row.compared_delta,
            row.matched_pairs, row.matched_a, row.matched_b, row.matched_delta,
        )


def _configuration_rows(config, source_a, source_b, sheet_a, sheet_b):
    yield "Source file", source_a, source_b, "Uploaded or bundled source name."
    yield "Worksheet", sheet_a, sheet_b, "Blank for CSV. File row is the original source position."
    for index, (key_a, key_b, kind) in enumerate(zip(config.source_a.keys, config.source_b.keys, config.key_types, strict=True), start=1):
        yield f"Key component {index}", key_a, key_b, f"{kind.title()} key comparison after normalization."
    yield "Row ID field", config.source_a.row_id, config.source_b.row_id, "Optional original identifier. File row is retained separately."
    yield "Amount field", config.source_a.amount, config.source_b.amount, "Finite decimal values in the same currency are compared."
    yield "Currency field", config.source_a.currency, config.source_b.currency, "Trimmed three-letter uppercase codes. No currency conversion."
    yield "Status field", config.source_a.status, config.source_b.status, "Trimmed text with case-sensitive comparison."
    yield "Amount tolerance", config.amount_tolerance, config.amount_tolerance, "Absolute A minus B amount must be at or below this value. Zero difference is exact."
    yield "Primary precedence", None, None, "Invalid key, duplicate key, missing counterpart, invalid value, currency mismatch, amount mismatch, status mismatch, within tolerance, exact match."
    yield "Issue flags", None, None, "All applicable flags are retained even if another issue determines the primary classification."
    yield "Impact", None, None, "High for duplicate or invalid keys, missing counterparts, invalid amounts or currencies, and currency mismatch. Review for other exceptions. Tolerated matches are labeled separately."
    yield "Match rate", None, None, _MATCH_RATE_NOTE
    yield "Currency totals", None, None, "Source totals include all valid amount and currency rows. Compared totals include valid same-currency pairs. Matched totals include exact and tolerated matches."
    yield "Unmatched amount", None, None, "Original signed amount on the present side of a missing-counterpart record, when amount and currency are valid."
    yield "Excel precision", None, None, _PRECISION_NOTE
    yield "Precision reference", None, None, _PRECISION_SOURCE
    yield "Text handling", None, None, "Evidence is stored as text. XML-incompatible characters are escaped. Values beyond Excel's text limit are marked as truncated."


def build_audit_workbook(
    result: ReconciliationResult,
    config: ReconciliationConfig,
    *,
    source_a: str,
    source_b: str,
    sheet_a: str | None = None,
    sheet_b: str | None = None,
    synthetic: bool = False,
) -> bytes:
    """Return a formatted, formula-free XLSX report from engine results."""
    if len(result.records) > _EXCEL_ROWS - _HEADER_ROW:
        raise AuditWorkbookError("The reconciliation results exceed the number of rows supported in an Excel worksheet.")

    workbook = Workbook(write_only=True)
    workbook.properties.creator = "ReconcileIQ"
    workbook.properties.title = "ReconcileIQ Audit Report"
    records = result.records
    metadata = (source_a, source_b, sheet_a, sheet_b)
    summary_note = (
        "Synthetic demo data · Counts and match coverage" if synthetic
        else "ReconcileIQ audit results · Counts and match coverage"
    )
    _table(
        workbook, "Executive Summary", summary_note,
        ("Category", "Measure", "Value", "Basis"), (20, 32, 18, 83),
        _summary_rows(result, config, source_a, source_b), wrap_columns=(3,), detail_height=32,
    )
    headers, widths = zip(*_RESULT_FIELDS, strict=True)
    _table(
        workbook, "Reconciliation Results", "All result records. See Audit Configuration for rules and Excel precision.",
        headers, widths, (_detail_row(index, record, *metadata) for index, record in enumerate(records, start=1)),
        fixed_columns=2,
    )
    _table(
        workbook, "Exceptions", "All records carrying issue flags, including accepted differences within tolerance.",
        headers, widths, (
            _detail_row(index, record, *metadata)
            for index, record in enumerate(records, start=1) if record.flags
        ), fixed_columns=2,
    )
    headers, widths = zip(*_DUPLICATE_FIELDS, strict=True)
    _table(
        workbook, "Duplicate Keys", "One row per quarantined source record. Filter by key to inspect the full group.",
        headers, widths, _duplicate_rows(records, *metadata), fixed_columns=1,
    )
    _table(
        workbook, "Data Quality", "Counts are based on physical source rows. Individual issues may overlap.",
        ("Measure", "Source A", "Source B", "Basis"), (40, 16, 16, 81), _quality_rows(result),
        wrap_columns=(3,), detail_height=24,
    )
    headers, widths = zip(*_CURRENCY_FIELDS, strict=True)
    _table(
        workbook, "Currency Summary", "Amounts stay separate by currency. Exact high-precision values appear as text in amber.",
        headers, widths, _currency_rows(result), fixed_columns=1,
    )
    _table(
        workbook, "Audit Configuration", "Source mappings and comparison rules applied to this report",
        ("Audit setting", "Source A", "Source B", "Rule or scope"), (26, 28, 28, 87),
        _configuration_rows(config, *metadata), wrap_columns=(1, 2, 3), detail_height=36,
    )
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
