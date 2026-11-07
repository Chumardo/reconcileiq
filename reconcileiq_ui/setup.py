"""Upload, map, and review transaction extracts before reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from json import JSONDecodeError

import pandas as pd
import streamlit as st

from reconcileiq import ConfigurationError, ReconciliationConfig, SchemaError, reconcile
from reconcileiq.inputs import InputError, UnsupportedCell

from .public_demo import demo_audit
from .sources import AuditRun, UploadLimits, list_worksheets, parse_upload, run_demo, validate_mapping


@dataclass(frozen=True, slots=True)
class _Upload:
    name: str
    content: bytes


@dataclass(frozen=True, slots=True)
class _Preview:
    upload: _Upload
    sheet: str | None
    sample: pd.DataFrame
    row_count: int


_FIELDS = (
    ("amount", "Amount", ("amount", "transaction_amount", "booked_amount", "settled_amount")),
    ("currency", "Currency", ("currency", "currency_code", "ccy")),
    ("status", "Status", ("status", "transaction_status", "ledger_status", "bank_status")),
    ("row_id", "Original row ID (optional)", ()),
)


def _replace_upload(side: str, upload: _Upload | None) -> None:
    state = st.session_state
    state[f"setup_file_{side}"] = upload
    for part in ("sheets", "sheet", "sheet_widget", "preview", "draft", "error"):
        state.pop(f"setup_{part}_{side}", None)
    for role in ("keys", *(field[0] for field in _FIELDS)):
        state.pop(f"setup_mapping_{side}_{role}", None)


def _capture_upload(side: str, widget: str, max_bytes: int) -> None:
    uploaded = st.session_state.get(widget)
    if uploaded is not None and uploaded.size > max_bytes:
        _replace_upload(side, None)
        st.session_state[f"setup_error_{side}"] = f"Choose a file under {max_bytes / 1024**2:g} MB."
        return
    upload = _Upload(uploaded.name, uploaded.getvalue()) if uploaded is not None else None
    _replace_upload(side, upload)


def _remove_upload(side: str) -> None:
    _replace_upload(side, None)
    revision_key = f"setup_upload_revision_{side}"
    st.session_state[revision_key] = st.session_state.get(revision_key, 0) + 1


def _finish_setup(run: AuditRun, on_run: Callable[[AuditRun], None]) -> None:
    for side in ("A", "B"):
        _remove_upload(side)
    for key in ("setup_key_types", "setup_tolerance_draft", "setup_amount_tolerance"):
        st.session_state.pop(key, None)
    on_run(run)


def _preview_cell(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, UnsupportedCell):
        return f"[{value.kind}] {value.value}"
    return str(value)


def _render_source(side: str, limits: UploadLimits) -> _Preview | None:
    state = st.session_state
    st.markdown(f"#### Source {side}")
    revision = state.get(f"setup_upload_revision_{side}", 0)
    widget = f"setup_upload_{side}_{revision}"
    st.file_uploader(
        f"Upload Source {side}", type=("csv", "xlsx"), key=widget,
        label_visibility="collapsed", on_change=_capture_upload, args=(side, widget, limits.max_bytes),
    )
    error = state.get(f"setup_error_{side}")
    if error:
        st.error(error)
    upload = state.get(f"setup_file_{side}")
    if upload is None:
        return None

    if state.get(widget) is None:
        st.text(f"{upload.name} · retained for this session")
        st.button(f"Remove Source {side}", key=f"setup_remove_{side}", on_click=_remove_upload, args=(side,))

    try:
        sheets_key = f"setup_sheets_{side}"
        if sheets_key not in state:
            state[sheets_key] = list_worksheets(upload.name, upload.content, limits=limits)
        sheets = state[sheets_key]
        sheet = None
        if sheets:
            sheet_key = f"setup_sheet_{side}"
            sheet_widget = f"setup_sheet_widget_{side}"
            if sheet_widget not in state:
                state[sheet_widget] = state.get(sheet_key, sheets[0])
            sheet = st.selectbox(f"Source {side} worksheet", sheets, key=sheet_widget)
            state[sheet_key] = sheet

        preview_key = f"setup_preview_{side}"
        preview = state.get(preview_key)
        if preview is None or preview.sheet != sheet:
            frame, _ = parse_upload(upload.name, upload.content, sheet=sheet, limits=limits)
            preview = _Preview(upload, sheet, frame.head(8).map(_preview_cell), len(frame))
            state[preview_key] = preview
    except (InputError, OSError) as error:
        st.error(f"Source {side} could not be read. Check the file and upload it again.")
        st.text(str(error))
        return None

    row_count, column_count = preview.row_count, len(preview.sample.columns)
    st.caption(f"{row_count:,} rows · {column_count:,} columns")
    with st.expander(f"Preview Source {side}"):
        if row_count:
            st.dataframe(preview.sample, width="stretch", hide_index=True)
            st.caption(f"First {len(preview.sample):,} of {row_count:,} rows · before audit validation")
        else:
            st.info("The file has headers and zero data rows. You can still reconcile it.")
    return preview


def _suggest_column(columns: list[str], aliases: tuple[str, ...]) -> str | None:
    candidates = [name for name in columns if name.strip().casefold() in aliases]
    return candidates[0] if len(candidates) == 1 else None


def _render_mapping(side: str, columns: list[str]) -> dict:
    state = st.session_state
    draft_key = f"setup_draft_{side}"
    draft = state.get(draft_key, {})
    st.markdown(f"#### Source {side} mapping")

    key_widget = f"setup_mapping_{side}_keys"
    current_keys = state.get(key_widget, draft.get("keys", ()))
    valid_keys = [name for name in current_keys if name in columns]
    if key_widget not in state or valid_keys != current_keys:
        state[key_widget] = valid_keys
    mapping = {"keys": st.multiselect(
        f"Source {side} key columns", columns, key=key_widget,
        placeholder="Select key columns in order",
    )}

    for role, label, aliases in _FIELDS:
        widget = f"setup_mapping_{side}_{role}"
        remembered = state.get(widget, draft.get(role, _suggest_column(columns, aliases)))
        if widget not in state or (remembered is not None and remembered not in columns):
            state[widget] = remembered if remembered in columns else None
        mapping[role] = st.selectbox(
            f"Source {side} {label.lower()}", columns, index=None, key=widget,
            placeholder="Choose a column" if role != "row_id" else "Use the file row number",
        )
    state[draft_key] = mapping
    return mapping


def _render_key_types(mapping_a: dict, mapping_b: dict) -> tuple[str, ...]:
    keys_a, keys_b = mapping_a["keys"], mapping_b["keys"]
    if not keys_a or len(keys_a) != len(keys_b):
        return ()
    st.markdown("#### Key component types")
    st.caption("Text preserves leading zeros and case. Integer keys normalize signs and leading zeros.")
    remembered = st.session_state.get("setup_key_types", {})
    key_types = {}
    for index, pair in enumerate(zip(keys_a, keys_b, strict=True), start=1):
        columns, control = st.columns([3, 2], vertical_alignment="center")
        columns.text(f"{index:02d}    {pair[0]}  ↔  {pair[1]}")
        widget = f"setup_key_type_{index}_{pair!r}"
        if widget not in st.session_state:
            st.session_state[widget] = remembered.get(pair, "text")
        key_types[pair] = control.selectbox(
            f"Key component {index} type", ("text", "integer"), key=widget,
            format_func=str.title, label_visibility="collapsed",
        )
    st.session_state["setup_key_types"] = key_types
    return tuple(key_types.values())


def _mapping_prompt(mapping_a: dict, mapping_b: dict) -> str | None:
    if not mapping_a["keys"] or not mapping_b["keys"]:
        return "Select at least one key column in each source."
    if len(mapping_a["keys"]) != len(mapping_b["keys"]):
        return "Select the same number of key columns in each source and pair them in order."
    gaps = []
    for side, mapping in (("A", mapping_a), ("B", mapping_b)):
        missing = [role for role in ("amount", "currency", "status") if mapping[role] is None]
        if missing:
            gaps.append(f"Source {side} ({', '.join(missing)})")
    return f"Choose the remaining columns for {' and '.join(gaps)}." if gaps else None


def _review_mapping(config: ReconciliationConfig) -> None:
    a, b = config.source_a, config.source_b
    fields = [
        (f"Key {index} · {kind.title()}", left, right)
        for index, (left, right, kind) in enumerate(zip(a.keys, b.keys, config.key_types, strict=True), start=1)
    ]
    fields.extend((label, getattr(a, role), getattr(b, role)) for role, label, _ in _FIELDS)
    frame = pd.DataFrame(fields, columns=["Audit field", "Source A", "Source B"]).fillna("File row number")
    with st.expander("Review mappings and audit rules", expanded=True):
        st.dataframe(frame, width="stretch", hide_index=True)
        st.caption(f"Amount tolerance: {config.amount_tolerance:f} in each transaction’s own currency.")
        st.caption(
            "Amounts are compared only in the same currency. Status matching is case-sensitive. "
            "Duplicate and invalid keys are quarantined."
        )


def _for_audit(preview: _Preview, amount_column: str, limits: UploadLimits) -> tuple[pd.DataFrame, tuple[int, ...]]:
    return parse_upload(
        preview.upload.name, preview.upload.content, sheet=preview.sheet, amount_column=amount_column, limits=limits,
    )


def render_setup(on_run: Callable[[AuditRun], None], *, public_demo: bool = False) -> None:
    """Render the setup page and hand a completed audit back to the app shell."""
    with st.container(border=True):
        copy, action = st.columns([3, 1.5], vertical_alignment="center")
        copy.markdown("#### Explore a sample audit")
        copy.caption("Use the bundled synthetic ledger and settlement files to see the exception workflow.")
        load_demo = action.button("Load demo dataset", key="setup_load_demo", width="stretch")
    if load_demo:
        try:
            with st.spinner("Reconciling the synthetic sources…"):
                run = demo_audit() if public_demo else run_demo()
        except (ConfigurationError, SchemaError, InputError, OSError, JSONDecodeError):
            st.error("The bundled demo could not be loaded. Try reloading the page.")
        else:
            if public_demo:
                on_run(run)
            else:
                _finish_setup(run, on_run)
            return

    if public_demo:
        st.caption("File uploads are disabled in the public demo.")
        return
    upload_limit = st.get_option("server.maxUploadSize")
    if upload_limit <= 0:
        st.info("To enable file uploads, launch the private workspace with an explicit upload limit.")
        st.code("streamlit run app.py --server.address 127.0.0.1 --server.maxUploadSize 15", language="text")
        return
    limits = UploadLimits(max_bytes=upload_limit * 1024**2)
    st.subheader("01 / Source files")
    st.caption("Upload two CSV or XLSX files with headers in the first row. CSV files must use UTF-8 and commas.")
    left, right = st.columns(2, gap="large")
    with left:
        source_a = _render_source("A", limits)
    with right:
        source_b = _render_source("B", limits)
    if source_a is None or source_b is None:
        st.caption("Add both source files to open column mapping.")
        return

    st.divider()
    st.subheader("02 / Column mapping")
    st.caption("Select key columns in the same order on both sides. Their header names can differ.")
    left, right = st.columns(2, gap="large")
    with left:
        mapping_a = _render_mapping("A", list(source_a.sample.columns))
    with right:
        mapping_b = _render_mapping("B", list(source_b.sample.columns))
    key_types = _render_key_types(mapping_a, mapping_b)

    st.divider()
    st.subheader("03 / Review and run")
    tolerance = st.text_input(
        "Amount tolerance", value=st.session_state.get("setup_tolerance_draft", "0"),
        key="setup_amount_tolerance", placeholder="0",
        help="Optional. Enter a nonnegative decimal such as 0.01. A difference exactly at the limit is accepted, "
             "and is shown separately from exact matches. The same limit applies in each currency's own units.",
    )
    st.session_state["setup_tolerance_draft"] = tolerance
    config = None
    prompt = _mapping_prompt(mapping_a, mapping_b)
    if prompt:
        st.info(prompt)
    else:
        try:
            config = validate_mapping(
                {"source_a": mapping_a, "source_b": mapping_b, "key_types": key_types,
                 "amount_tolerance": tolerance.strip() or "0"},
                source_a.sample.columns, source_b.sample.columns,
            )
        except ConfigurationError as error:
            st.error(f"Review the column mapping. {error}")
        else:
            _review_mapping(config)

    if st.button("Run reconciliation", type="primary", key="setup_run", disabled=config is None):
        try:
            with st.spinner("Validating rows and reconciling keys…"):
                frame_a, locators_a = _for_audit(source_a, config.source_a.amount, limits)
                frame_b, locators_b = _for_audit(source_b, config.source_b.amount, limits)
                result = reconcile(frame_a, frame_b, config, row_numbers_a=locators_a, row_numbers_b=locators_b)
        except (InputError, SchemaError, OSError) as error:
            st.error("The audit could not run. Review the source files and mappings.")
            st.text(str(error))
        else:
            _finish_setup(AuditRun(
                result, source_a.upload.name, source_b.upload.name, config,
                sheet_a=source_a.sheet, sheet_b=source_b.sheet,
            ), on_run)
