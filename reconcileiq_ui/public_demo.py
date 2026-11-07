"""Process-wide resources for the fixed synthetic audit."""

import streamlit as st

from reconcileiq.audit_workbook import build_audit_workbook

from .sources import AuditRun, run_demo


@st.cache_resource(max_entries=1, show_spinner=False)
def demo_audit() -> AuditRun:
    # Views consume this synthetic result as read-only. Session uploads stay separate.
    return run_demo()


@st.cache_resource(max_entries=1, show_spinner=False)
def demo_workbook() -> bytes:
    run = demo_audit()
    return build_audit_workbook(
        run.result, run.config, source_a=run.source_a, source_b=run.source_b, synthetic=True,
    )
