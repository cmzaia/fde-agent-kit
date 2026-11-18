"""hello_agent: support-ticket triage as a tiny LangGraph, wired through the whole kit.

record (any Connector) -> triage (LLM, structured output, retries, cost-tracked)
                       -> confident?  yes: auto-resolve   no: human review queue
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from fde_kit.config import get_settings
from fde_kit.llm import get_llm, structured_output
from fde_kit.observability import external_callbacks, trace_id_var
from fde_kit.review_queue import ReviewQueue

REVIEW_BELOW = 0.7


class Triage(BaseModel):
    category: Literal["billing", "outage", "access", "other"]
    priority: Literal["p1", "p2", "p3"]
    confidence: float = Field(ge=0, le=1)
    summary: str


class State(TypedDict, total=False):
    text: str
    triage: dict[str, Any]
    needs_review: bool
    action: str
    review_id: int | None


def build_graph(llm: BaseChatModel, queue: ReviewQueue | None = None):
    retries = get_settings().llm_max_retries

    def triage(state: State) -> State:
        result = structured_output(
            llm,
            Triage,
            f"Triage this support ticket.\nTICKET:\n{state['text']}\nEND_TICKET",
            system="You triage customer support tickets.",
            retries=retries,
        )
        return {"triage": result.model_dump()}

    def decide(state: State) -> Literal["auto_resolve", "escalate"]:
        return "escalate" if state["triage"]["confidence"] < REVIEW_BELOW else "auto_resolve"

    def auto_resolve(state: State) -> State:
        return {"needs_review": False, "action": "auto_routed", "review_id": None}

    def escalate(state: State) -> State:
        review_id = None
        if queue is not None:
            review_id = queue.enqueue(
                "ticket_triage", {"text": state["text"], **state["triage"]}, trace_id_var.get()
            )
        return {"needs_review": True, "action": "queued_for_review", "review_id": review_id}

    g = StateGraph(State)
    g.add_node("triage", triage)
    g.add_node("auto_resolve", auto_resolve)
    g.add_node("escalate", escalate)
    g.add_edge(START, "triage")
    g.add_conditional_edges("triage", decide)
    g.add_edge("auto_resolve", END)
    g.add_edge("escalate", END)
    return g.compile()


def run(
    inp: dict[str, Any], llm: BaseChatModel | None = None, queue: ReviewQueue | None = None
) -> dict[str, Any]:
    """Eval-friendly entry point: {"text": ...} -> flat result dict."""
    graph = build_graph(llm or get_llm(), queue)
    out = graph.invoke({"text": inp["text"]}, config={"callbacks": external_callbacks()})
    return {
        **out["triage"],
        "needs_review": out["needs_review"],
        "action": out["action"],
        "review_id": out["review_id"],
    }
