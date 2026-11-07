from pathlib import Path

import pytest


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def open_app():
    testing = pytest.importorskip("streamlit.testing.v1")
    app = testing.AppTest.from_file(str(APP_PATH), default_timeout=60).run()
    assert not app.exception
    return app


def show_view(app, view):
    app.radio(key="workspace_view").set_value(view).run()
    assert not app.exception


def test_demo_opens_overview_and_keeps_results_across_workspace_views():
    app = open_app()
    assert app.title[0].value == "Reconciliation setup"

    show_view(app, "Overview")
    assert any("Run an audit from Setup" in info.value for info in app.info)

    show_view(app, "Setup")
    app.button(key="setup_load_demo").click().run()
    assert not app.exception
    assert not app.error
    assert app.radio(key="workspace_view").value == "Overview"
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Source A rows"] == "10,001"
    assert metrics["Source B rows"] == "10,000"
    assert metrics["Match rate"] == "99.88%"

    show_view(app, "Exception Explorer")
    assert app.title[0].value == "Exception explorer"
    assert app.dataframe
    app.text_input(key="record_query_1").set_value("does-not-exist").run()
    assert not app.exception
    assert any("No records match these filters" in info.value for info in app.info)

    show_view(app, "Data Quality")
    assert app.title[0].value == "Data quality"
    assert [heading.value for heading in app.subheader] == [
        "Data Quality Issue Matrix", "Missing required values", "Reconciliation exclusions",
    ]
    assert any("totals count issue occurrences" in caption.value for caption in app.caption)


def test_uploaded_csv_sources_can_run_with_mapped_composite_keys(private_uploads):
    app = open_app()
    if not hasattr(app, "file_uploader"):
        pytest.skip("Installed Streamlit AppTest does not expose file uploads")

    source_a = (
        b"merchant,txn,amount,status,currency\n"
        b"North,001,0.10,SETTLED,USD\n"
        b"North,002,2.05,SETTLED,EUR\n"
    )
    source_b = (
        b"party,payment,amount,status,currency\n"
        b"North,1,0.10,SETTLED,USD\n"
        b"North,2,2.01,FAILED,EUR\n"
    )
    app.file_uploader(key="setup_upload_A_0").set_value(("ledger.csv", source_a, "text/csv"))
    app.file_uploader(key="setup_upload_B_0").set_value(("bank.csv", source_b, "text/csv"))
    app.run()
    assert not app.exception
    assert app.button(key="setup_run").disabled

    app.multiselect(key="setup_mapping_A_keys").set_value(["merchant", "txn"])
    app.multiselect(key="setup_mapping_B_keys").set_value(["party", "payment"])
    app.run()
    assert not app.exception
    assert not app.button(key="setup_run").disabled

    kind = next(widget for widget in app.selectbox if widget.label == "Key component 2 type")
    kind.set_value("integer")
    app.button(key="setup_run").click().run()
    assert not app.exception
    assert not app.error
    assert app.radio(key="workspace_view").value == "Overview"
    assert {metric.label: metric.value for metric in app.metric}["Match rate"] == "50.00%"
