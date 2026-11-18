import io
import json
import logging
import sys
import types

from fde_kit.observability import (
    UsageTracker,
    active_integrations,
    configure_logging,
    estimate_cost,
    external_callbacks,
    trace_id_var,
    trace_run,
)


def test_json_logs_carry_trace_id():
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    with trace_run("demo") as ctx:
        logging.getLogger("x").info("hello", extra={"k": 1})
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    hello = next(line for line in lines if line["msg"] == "hello")
    assert hello["trace_id"] == ctx.trace_id and hello["k"] == 1
    end = next(line for line in lines if line["msg"] == "run.end")
    assert "latency_s" in end and "cost_usd" in end


def test_trace_id_unique_and_reset():
    with trace_run() as a:
        pass
    with trace_run() as b:
        pass
    assert a.trace_id != b.trace_id and trace_id_var.get() is None


def test_cost_math():
    assert estimate_cost("claude-sonnet-5-5", 1_000_000, 1_000_000) == 18.0
    assert estimate_cost("unknown-model", 10, 10) == 0.0
    assert estimate_cost("unknown-model", 1_000_000, 0, price_in=2.0) == 2.0


def test_usage_tracker_accumulates():
    t = UsageTracker()
    t.add("fake-triage-1", 100, 50)
    t.add("fake-triage-1", 100, 50)
    assert t.calls == 2 and t.input_tokens == 200 and t.cost_usd > 0


def test_integrations_off_by_default():
    assert active_integrations({}) == []
    assert external_callbacks() == []


def test_integrations_switch_on_with_env():
    env = {
        "LANGFUSE_PUBLIC_KEY": "a",
        "LANGFUSE_SECRET_KEY": "b",
        "LANGSMITH_API_KEY": "c",
        "LANGSMITH_TRACING": "true",
    }
    assert active_integrations(env) == ["langfuse", "langsmith"]


def test_langfuse_handler_used_when_available(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "a")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "b")
    fake = types.ModuleType("langfuse.langchain")
    fake.CallbackHandler = lambda: "handler"
    monkeypatch.setitem(sys.modules, "langfuse", types.ModuleType("langfuse"))
    monkeypatch.setitem(sys.modules, "langfuse.langchain", fake)
    assert external_callbacks() == ["handler"]
