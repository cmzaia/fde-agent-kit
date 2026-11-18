"""Provider factory (fake / anthropic / openai) and a retrying structured-output helper."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel, ValidationError

from fde_kit.config import Settings, get_settings
from fde_kit.observability import record_llm_usage

log = logging.getLogger("fde_kit.llm")


class LLMConfigError(RuntimeError):
    """Provider is misconfigured (missing key, missing extra, unknown name)."""


class StructuredOutputError(RuntimeError):
    """The model never produced output matching the schema within the retry budget."""


# --------------------------------------------------------------------------- fake model

_RULES: list[tuple[str, str, list[str]]] = [
    ("outage", "p1", ["outage", "is down", "are down", "500 error", "unreachable", "crash"]),
    ("billing", "p2", ["invoice", "charge", "refund", "payment", "billed", "receipt"]),
    ("access", "p2", ["password", "login", "log in", "locked out", "2fa", "sso"]),
]


def _tokens(text: str) -> int:
    return max(1, len(text.split()))


def _fake_answer(prompt: str) -> dict[str, Any]:
    """Deterministic, keyword-driven stand-in for a model. Same input, same output, no network."""
    if "JUDGE:" in prompt:
        exp = re.search(r"EXPECTED: (.*)", prompt)
        act = re.search(r"ACTUAL: (.*)", prompt)
        ok = False
        if exp and act:
            want, got = json.loads(exp.group(1)), json.loads(act.group(1))
            ok = all(
                isinstance(v, dict) or str(got.get(k)).lower() == str(v).lower()
                for k, v in want.items()
            )
        return {"score": 1.0 if ok else 0.0, "reason": "fake judge: expected fields present"}
    m = re.search(r"TICKET:\n(.*?)\nEND_TICKET", prompt, re.S)
    ticket = (m.group(1) if m else prompt).strip()
    low = ticket.lower()
    for category, priority, words in _RULES:
        if any(w in low for w in words):
            return {
                "category": category,
                "priority": priority,
                "confidence": 0.95,
                "summary": ticket[:80],
            }
    return {"category": "other", "priority": "p3", "confidence": 0.4, "summary": ticket[:80]}


class FakeChatModel(BaseChatModel):
    """Offline chat model used when LLM_PROVIDER=fake (tests, CI, demos without a key).

    `bad_first` makes the first N replies invalid JSON so retry logic can be exercised.
    """

    model_name: str = "fake-triage-1"
    bad_first: int = 0
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake-triage"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls += 1
        prompt = "\n".join(str(m.content) for m in messages)
        text = "not json" if self.calls <= self.bad_first else json.dumps(_fake_answer(prompt))
        usage = {
            "input_tokens": _tokens(prompt),
            "output_tokens": _tokens(text),
            "total_tokens": _tokens(prompt) + _tokens(text),
        }
        msg = AIMessage(content=text, usage_metadata=usage)  # type: ignore[arg-type]
        return ChatResult(generations=[ChatGeneration(message=msg)])


# --------------------------------------------------------------------------- factory


def get_llm(settings: Settings | None = None) -> BaseChatModel:
    """Build the chat model named by LLM_PROVIDER. Provider SDKs are imported lazily."""
    s = settings or get_settings()
    provider, model = s.llm_provider.lower(), s.model_name
    if provider == "fake":
        return FakeChatModel(model_name=model)
    if provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMConfigError("LLM_PROVIDER=anthropic needs ANTHROPIC_API_KEY")
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as e:
            raise LLMConfigError("install the extra: uv sync --extra anthropic") from e
        return ChatAnthropic(  # type: ignore[call-arg]
            model=model, temperature=s.llm_temperature, max_retries=s.llm_max_retries
        )
    if provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            raise LLMConfigError("LLM_PROVIDER=openai needs OPENAI_API_KEY")
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as e:
            raise LLMConfigError("install the extra: uv sync --extra openai") from e
        return ChatOpenAI(model=model, temperature=s.llm_temperature, max_retries=s.llm_max_retries)
    raise LLMConfigError(f"unknown LLM_PROVIDER {provider!r} (fake | anthropic | openai)")


def model_label(llm: BaseChatModel) -> str:
    return str(getattr(llm, "model_name", None) or getattr(llm, "model", None) or llm._llm_type)


# --------------------------------------------------------------------------- structured output


def _extract_json(text: str) -> Any:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in reply")
    return json.loads(text[start : end + 1])


def structured_output[T: BaseModel](
    llm: BaseChatModel,
    schema: type[T],
    prompt: str,
    *,
    system: str = "You are a precise extraction engine.",
    retries: int = 3,
    config: dict[str, Any] | None = None,
) -> T:
    """Ask for JSON matching `schema`; on parse/validation failure, feed the error back and retry.

    Works with any chat model (no reliance on provider-specific tool-calling), and every
    attempt is token-accounted so retries show up in the cost report.
    """
    sys_msg = (
        f"{system}\nReply with ONLY a JSON object matching this JSON Schema, no prose:\n"
        f"{json.dumps(schema.model_json_schema())}"
    )
    messages: list[BaseMessage] = [SystemMessage(sys_msg), HumanMessage(prompt)]
    label, last_error = model_label(llm), "unknown"
    for attempt in range(retries + 1):
        reply = llm.invoke(messages, config=config)  # type: ignore[arg-type]
        record_llm_usage(label, getattr(reply, "usage_metadata", None))
        raw = str(reply.content)
        try:
            return schema.model_validate(_extract_json(raw))
        except (ValueError, ValidationError) as e:
            last_error = str(e).splitlines()[0][:300]
            log.warning(
                "structured_output.retry", extra={"attempt": attempt + 1, "error": last_error}
            )
            messages += [
                AIMessage(raw),
                HumanMessage(f"That was invalid ({last_error}). Reply with only valid JSON."),
            ]
    raise StructuredOutputError(
        f"{schema.__name__}: no valid output after {retries + 1} attempts: {last_error}"
    )
