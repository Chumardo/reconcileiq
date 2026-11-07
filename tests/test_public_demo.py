"""Check the public entrypoint, its fixed inputs, and its synthetic export."""

from io import BytesIO
from pathlib import Path
import tomllib

from openpyxl import load_workbook
import pytest

from reconcileiq.inputs import InputError


PROJECT = Path(__file__).resolve().parents[1]


def test_default_public_server_rejects_nonempty_uploads_and_hides_error_details():
    config = tomllib.loads((PROJECT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert config["server"]["maxUploadSize"] == 0
    assert config["client"]["showErrorDetails"] == "none"


def test_public_entrypoint_uses_only_the_bundled_run_even_with_uploads_configured(private_uploads):
    testing = pytest.importorskip("streamlit.testing.v1")
    app = testing.AppTest.from_file(str(PROJECT / "demo_app.py"), default_timeout=60)
    app.session_state["active_audit"] = "private audit sentinel"
    app.run()
    assert not app.exception
    assert not app.get("file_uploader")
    assert any("disabled in the public demo" in caption.value for caption in app.caption)

    app.button(key="setup_load_demo").click().run()
    assert not app.exception
    assert app.radio(key="workspace_view").value == "Overview"
    assert app.session_state["public_demo_loaded"] is True
    assert app.session_state["active_audit"] == "private audit sentinel"
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Matched"] == "9,986"
    assert metrics["Eligible exceptions"] == "12"
    assert metrics["Quarantined / excluded"] == "9"
    assert metrics["Match rate"] == "99.88%"
    assert any("9,986 of 9,998 eligible" in caption.value for caption in app.caption)

    app.radio(key="workspace_view").set_value("Exception Explorer").run()
    app.text_input(key="record_query_1").set_value("SYNTH-001 / 000001").run()
    assert not app.exception
    assert len(app.dataframe[0].value) == 1
    assert app.dataframe[0].value.iloc[0]["Primary outcome"] == "Missing in A"

    app.radio(key="workspace_view").set_value("Data Quality").run()
    assert not app.exception
    assert any(item.value == "Issue counts by source" for item in app.subheader)

    app.button(key="clear_audit").click().run()
    assert app.radio(key="workspace_view").value == "Setup"
    assert not app.get("file_uploader")


def test_failed_demo_load_keeps_local_paths_out_of_the_message(monkeypatch):
    testing = pytest.importorskip("streamlit.testing.v1")
    from reconcileiq_ui import setup

    def unavailable():
        raise OSError(r"C:\private\sensitive_file.csv")

    monkeypatch.setattr(setup, "demo_audit", unavailable)
    app = testing.AppTest.from_file(str(PROJECT / "demo_app.py")).run()
    app.button(key="setup_load_demo").click().run()
    assert not app.exception
    assert [error.value for error in app.error] == ["The bundled demo could not be loaded. Try reloading the page."]


def test_bundled_demo_checks_the_byte_limit_before_parsing(tmp_path):
    from reconcileiq_ui.sources import _read_demo_source

    fixture = tmp_path / "source.csv"
    fixture.write_bytes(b"x" * (1024**2 + 1))
    with pytest.raises(InputError, match="upload size limit"):
        _read_demo_source(fixture)


def test_public_workbook_uses_one_cached_synthetic_report():
    pytest.importorskip("streamlit")
    from reconcileiq_ui.public_demo import demo_audit, demo_workbook

    first = demo_workbook()
    assert demo_workbook() is first
    run = demo_audit()
    assert run.is_demo
    assert (run.source_a, run.source_b) == ("source_a.csv", "source_b.csv")

    with BytesIO(first) as file:
        workbook = load_workbook(file, read_only=True)
        assert len(workbook.sheetnames) == 7
        summary = workbook["Executive Summary"]
        assert "Synthetic demo data" in summary["A3"].value
        metrics = {row[1]: row[2] for row in summary.iter_rows(min_row=6, values_only=True)}
        assert metrics["Exact matches"] == run.result.summary["exact_matched_records"]
        assert metrics["Match-rate denominator"] == run.result.summary["eligible_records"]
        workbook.close()
