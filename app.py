"""Launch the ReconcileIQ workspace with `streamlit run app.py`."""

from dataclasses import asdict
from functools import partial
from html import escape
from pathlib import Path

import streamlit as st

from reconcileiq.audit_workbook import build_audit_workbook
from reconcileiq_ui.dashboard import render_dashboard
from reconcileiq_ui.explorer import render_explorer
from reconcileiq_ui.public_demo import demo_audit, demo_workbook
from reconcileiq_ui.quality import render_quality
from reconcileiq_ui.setup import render_setup
from reconcileiq_ui.sources import AuditRun


_STYLES = Path(__file__).with_name("reconcileiq_ui") / "workspace.css"
_VIEWS = ("Setup", "Overview", "Exception Explorer", "Data Quality")
_PAGE_COPY = {
    "Setup": ("Reconciliation setup", "Add two transaction files, map their columns, and run an audit."),
    "Overview": ("Reconciliation overview", "Review match coverage, open exceptions, and currency differences."),
    "Exception Explorer": ("Exception explorer", "Find a transaction and inspect its source values and original row references."),
    "Data Quality": ("Data quality", "See which source rows have validation issues and how those issues affect the audit."),
}


def _accept_run(run: AuditRun, *, public_demo: bool) -> None:
    if public_demo:
        st.session_state["public_demo_loaded"] = True
    else:
        st.session_state["active_audit"] = run
    st.session_state["audit_serial"] = st.session_state.get("audit_serial", 0) + 1
    st.session_state["pending_view"] = "Overview"
    st.rerun()


def _source_label(filename: str, worksheet: str | None) -> str:
    return f"{filename} · {worksheet}" if worksheet else filename


def _sidebar(run: AuditRun | None, *, public_demo: bool) -> str:
    with st.sidebar:
        st.html("""
            <div class="ri-brand">
              <span class="ri-brand-mark" aria-hidden="true">⇄</span>
              <div><strong>ReconcileIQ</strong><small>Transaction audit</small></div>
            </div>
        """)
        st.caption("WORKSPACE")
        view = st.radio("Workspace view", _VIEWS, key="workspace_view", label_visibility="collapsed")
        st.divider()
        if run is None:
            st.caption("No active audit")
        else:
            st.badge("Synthetic demo" if run.is_demo else "Audit ready", icon=":material/check_circle:", color="green")
            st.caption(f"{run.result.summary['result_records']:,} reconciliation records")
            if st.button("Clear audit", key="clear_audit", icon=":material/close:"):
                audit_key = "public_demo_loaded" if public_demo else "active_audit"
                st.session_state.pop(audit_key, None)
                st.session_state["pending_view"] = "Setup"
                st.rerun()
        return view


def _page_header(view: str, run: AuditRun | None, *, public_demo: bool) -> None:
    title, description = _PAGE_COPY[view]
    if public_demo and view == "Setup":
        description = "Explore the results and download an audit of the bundled transactions."
    st.title(title, anchor=False)
    if public_demo:
        st.badge("Public demo · Synthetic data only", color="green")
    elif run is not None and run.is_demo:
        st.badge("Synthetic data", color="green")
    st.caption(description)
    if run is None or view == "Setup":
        return
    if view == "Overview":
        labels, settings, download = st.columns((3.5, 1.2, 1.7), vertical_alignment="center")
    else:
        labels, settings = st.columns((4, 1), vertical_alignment="center")
        download = None
    with labels:
        st.html(f"""
            <div class="ri-source-strip" aria-label="Active audit sources">
              <span><b>Source A</b> {escape(_source_label(run.source_a, run.sheet_a))}</span>
              <span><b>Source B</b> {escape(_source_label(run.source_b, run.sheet_b))}</span>
            </div>
        """)
    with settings.popover("Run settings", width="stretch"):
        st.caption("Column mappings, key types, and amount tolerance used for this audit.")
        config = asdict(run.config)
        config["amount_tolerance"] = format(run.config.amount_tolerance, "f")
        st.json(config, expanded=2)
    if download is not None:
        download.download_button(
            "Download XLSX",
            data=demo_workbook if public_demo else partial(
                build_audit_workbook, run.result, run.config, source_a=run.source_a, source_b=run.source_b,
                sheet_a=run.sheet_a, sheet_b=run.sheet_b, synthetic=run.is_demo,
            ),
            file_name="reconcileiq-demo-audit.xlsx" if run.is_demo else "reconcileiq-audit.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            on_click="ignore", icon=":material/download:", width="stretch", type="primary",
            help="Export the full audit, including every result and the applied comparison rules.",
        )


def main(*, public_demo: bool = False) -> None:
    st.set_page_config(page_title="ReconcileIQ", page_icon=":material/swap_horiz:", layout="wide")
    st.html(_STYLES)
    pending_view = st.session_state.pop("pending_view", None)
    if pending_view is not None:
        st.session_state["workspace_view"] = pending_view

    if public_demo:
        run = demo_audit() if st.session_state.get("public_demo_loaded") else None
    else:
        run = st.session_state.get("active_audit")
    view = _sidebar(run, public_demo=public_demo)
    _page_header(view, run, public_demo=public_demo)
    if view == "Setup":
        render_setup(partial(_accept_run, public_demo=public_demo), public_demo=public_demo)
    elif run is None:
        message = (
            "Load the synthetic demo from Setup to view results." if public_demo
            else "Run an audit from Setup to view results. You can also load the synthetic demo."
        )
        st.info(message)
        if st.button("Go to setup", icon=":material/arrow_back:"):
            st.session_state["pending_view"] = "Setup"
            st.rerun()
    elif view == "Overview":
        render_dashboard(run.result)
    elif view == "Exception Explorer":
        render_explorer(
            run.result, st.session_state["audit_serial"], config=run.config,
            source_a=_source_label(run.source_a, run.sheet_a), source_b=_source_label(run.source_b, run.sheet_b),
        )
    else:
        render_quality(run.result)


if __name__ == "__main__":
    main()
