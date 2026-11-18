"""Structured logging, per-run trace ids, token/cost accounting, optional tracing hooks."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Illustrative prices in USD per million tokens (input, output). Real contracts differ:
# override with LLM_PRICE_IN_PER_MTOK / LLM_PRICE_OUT_PER_MTOK.
PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (3.0, 15.0),
    "gpt-4o-mini": (0.15, 0.60),
    "fake-triage-1": (3.0, 15.0),
}

_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}

trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
usage_var: contextvars.ContextVar[UsageTracker | None] = contextvars.ContextVar(
    "usage", default=None
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the current trace id and any `extra=` fields."""

    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": trace_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                doc[key] = value
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str)


def configure_logging(level: str = "INFO", stream: Any = None) -> None:
    """Idempotently route the root logger through the JSON formatter."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_fde", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler._fde = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level.upper())


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    price_in: float | None = None,
    price_out: float | None = None,
) -> float:
    p_in, p_out = PRICES.get(model, (0.0, 0.0))
    p_in = price_in if price_in is not None else p_in
    p_out = price_out if price_out is not None else p_out
    return (input_tokens * p_in + output_tokens * p_out) / 1_000_000


@dataclass
class UsageTracker:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, int] = field(default_factory=dict)

    def add(self, model: str, input_tokens: int, output_tokens: int) -> None:
        from fde_kit.config import get_settings

        s = get_settings()
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.calls += 1
        self.by_model[model] = self.by_model.get(model, 0) + input_tokens + output_tokens
        self.cost_usd += estimate_cost(
            model, input_tokens, output_tokens, s.llm_price_in_per_mtok, s.llm_price_out_per_mtok
        )


def record_llm_usage(model: str, usage_metadata: dict[str, Any] | None) -> None:
    """Called after every LLM call; a no-op outside a run context."""
    tracker = usage_var.get()
    if tracker is None or not usage_metadata:
        return
    tracker.add(
        model,
        int(usage_metadata.get("input_tokens", 0)),
        int(usage_metadata.get("output_tokens", 0)),
    )


@dataclass
class RunContext:
    trace_id: str
    name: str
    usage: UsageTracker
    started: float = field(default_factory=time.perf_counter)

    @property
    def latency_s(self) -> float:
        return time.perf_counter() - self.started


@contextlib.contextmanager
def trace_run(name: str = "run", trace_id: str | None = None) -> Iterator[RunContext]:
    """Open a run: assigns a trace id, tracks tokens/cost, logs start/end with latency."""
    ctx = RunContext(trace_id or uuid.uuid4().hex[:16], name, UsageTracker())
    t_tok = trace_id_var.set(ctx.trace_id)
    u_tok = usage_var.set(ctx.usage)
    log = logging.getLogger("fde_kit.run")
    log.info("run.start", extra={"run": name})
    try:
        yield ctx
    except Exception:
        log.exception("run.error", extra={"run": name})
        raise
    finally:
        log.info(
            "run.end",
            extra={
                "run": name,
                "latency_s": round(ctx.latency_s, 4),
                "input_tokens": ctx.usage.input_tokens,
                "output_tokens": ctx.usage.output_tokens,
                "cost_usd": round(ctx.usage.cost_usd, 6),
            },
        )
        trace_id_var.reset(t_tok)
        usage_var.reset(u_tok)


def active_integrations(env: dict[str, str] | None = None) -> list[str]:
    """Which tracing backends are switched on by the environment (nothing is on by default)."""
    env = dict(os.environ) if env is None else env
    found = []
    if env.get("LANGFUSE_PUBLIC_KEY") and env.get("LANGFUSE_SECRET_KEY"):
        found.append("langfuse")
    key = env.get("LANGSMITH_API_KEY") or env.get("LANGCHAIN_API_KEY")
    tracing = env.get("LANGSMITH_TRACING") or env.get("LANGCHAIN_TRACING_V2")
    if key and str(tracing).lower() == "true":
        found.append("langsmith")
    return found


def external_callbacks() -> list[Any]:
    """LangChain callback handlers for enabled backends. LangSmith hooks in natively via env."""
    handlers: list[Any] = []
    if "langfuse" in active_integrations():
        try:
            try:
                from langfuse.langchain import CallbackHandler  # langfuse >= 3
            except ImportError:
                from langfuse.callback import CallbackHandler  # type: ignore[no-redef]
            handlers.append(CallbackHandler())
        except ImportError:
            logging.getLogger("fde_kit").warning(
                "LANGFUSE_* set but langfuse is not installed; run with --extra langfuse"
            )
    return handlers
