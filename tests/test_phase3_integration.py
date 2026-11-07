"""Exercise the report flow at the workspace and CLI boundaries."""

import csv
import json
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from reconcileiq import ColumnMapping, ReconciliationConfig, reconcile
from reconcileiq.export import write_outputs


def test_csv_neutralizes_spreadsheet_text_without_changing_json_evidence(tmp_path):
    columns = ["key", "amount", "status", "currency", "row_id"]
    mapping = ColumnMapping(("key",), "amount", "status", "currency", "row_id")
    frame = pd.DataFrame([
        ["0001", "-12.34", "@SUM(1,2)", "USD", " \t=1+1"],
        ["0002", "0", "SETTLED", "EUR", "00042"],
    ], columns=columns)
    write_outputs(reconcile(frame, frame, ReconciliationConfig(mapping, mapping)), tmp_path)

    with (tmp_path / "records.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["source_a_row_id"] == "' \t=1+1"
    assert rows[0]["source_b_status"] == "'@SUM(1,2)"
    assert rows[0]["source_a_amount"] == "-12.34"
    assert rows[1]["source_a_row_id"] == "00042"

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    evidence = report["records"][0]["source_a"]
    assert evidence["row_id"] == " \t=1+1"
    assert evidence["raw"]["status"] == "@SUM(1,2)"


def test_workspace_validates_and_applies_tolerance_then_releases_upload_drafts(private_uploads):
    testing = pytest.importorskip("streamlit.testing.v1")
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    app = testing.AppTest.from_file(str(app_path), default_timeout=60).run()
    assert not app.exception
    if not hasattr(app, "file_uploader"):
        pytest.skip("Installed Streamlit AppTest does not expose file uploads")

    header = b"txn,amount,status,currency\n"
    app.file_uploader(key="setup_upload_A_0").set_value(("a.csv", header + b"01,1.23,PAID,USD\n", "text/csv"))
    app.file_uploader(key="setup_upload_B_0").set_value(("b.csv", header + b"01,1.22,PAID,USD\n", "text/csv"))
    app.run()
    app.multiselect(key="setup_mapping_A_keys").set_value(["txn"])
    app.multiselect(key="setup_mapping_B_keys").set_value(["txn"])
    app.text_input(key="setup_amount_tolerance").set_value("-0.01").run()
    assert not app.exception
    assert app.button(key="setup_run").disabled
    assert any("nonnegative decimal" in error.value for error in app.error)

    app.text_input(key="setup_amount_tolerance").set_value("0.01").run()
    app.button(key="setup_run").click().run()
    assert not app.exception
    assert app.radio(key="workspace_view").value == "Overview"
    assert {metric.label: metric.value for metric in app.metric}["Match rate"] == "100.00%"
    audit = app.session_state["active_audit"]
    assert audit.config.amount_tolerance == Decimal("0.01")
    assert audit.result.records[0].classification == "matched_within_tolerance"
    assert audit.result.records[0].amount_delta == Decimal("0.01")
    assert app.session_state["setup_file_A"] is None
    assert app.session_state["setup_file_B"] is None
