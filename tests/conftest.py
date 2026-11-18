import pytest

from fde_kit.config import get_settings


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch):
    """Every test runs offline: no key, no network."""
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    for k in (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGSMITH_API_KEY",
        "LANGCHAIN_API_KEY",
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING_V2",
    ):
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
