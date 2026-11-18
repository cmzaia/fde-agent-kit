import pytest
from examples.hello_agent.agent import Triage
from pydantic import BaseModel

from fde_kit.config import Settings
from fde_kit.llm import (
    FakeChatModel,
    LLMConfigError,
    StructuredOutputError,
    get_llm,
    structured_output,
)
from fde_kit.observability import trace_run


def prompt(text):
    return f"TICKET:\n{text}\nEND_TICKET"


def test_factory_fake_is_default():
    assert isinstance(get_llm(Settings()), FakeChatModel)


def test_factory_unknown_provider():
    with pytest.raises(LLMConfigError, match="unknown"):
        get_llm(Settings(llm_provider="nope"))


def test_factory_anthropic_needs_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMConfigError, match="ANTHROPIC_API_KEY"):
        get_llm(Settings(llm_provider="anthropic"))


def test_factory_anthropic_default_model(monkeypatch):
    pytest.importorskip("langchain_anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    llm = get_llm(Settings(llm_provider="anthropic"))
    assert llm.model == "claude-sonnet-5-5"


def test_factory_openai(monkeypatch):
    pytest.importorskip("langchain_openai")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    assert get_llm(Settings(llm_provider="openai")).model_name == "gpt-4o-mini"


def test_structured_output_parses():
    out = structured_output(FakeChatModel(), Triage, prompt("My invoice is wrong"))
    assert out.category == "billing" and out.priority == "p2"


def test_structured_output_retries_then_succeeds():
    llm = FakeChatModel(bad_first=2)
    out = structured_output(llm, Triage, prompt("site is down"), retries=3)
    assert out.category == "outage"
    assert llm.calls == 3


def test_structured_output_gives_up():
    with pytest.raises(StructuredOutputError):
        structured_output(FakeChatModel(bad_first=99), Triage, prompt("x"), retries=2)


def test_structured_output_validates_schema():
    class Other(BaseModel):
        nonexistent: int

    with pytest.raises(StructuredOutputError):
        structured_output(FakeChatModel(), Other, prompt("x"), retries=1)


def test_retries_are_cost_accounted():
    with trace_run("t") as ctx:
        structured_output(FakeChatModel(bad_first=1), Triage, prompt("refund me"))
    assert ctx.usage.calls == 2 and ctx.usage.cost_usd > 0
