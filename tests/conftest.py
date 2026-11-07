import pytest


@pytest.fixture
def private_uploads(monkeypatch):
    """Apply the upload limit used by the documented private launch command."""
    streamlit = pytest.importorskip("streamlit")
    testing = pytest.importorskip("streamlit.testing.v1.util")
    with testing.patch_config_options({"server.maxUploadSize": 15}):
        monkeypatch.setattr(streamlit, "get_option", streamlit.config.get_option)
        yield
