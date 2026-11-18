"""End-to-end demo: SQL connector -> agent -> review queue. Runs offline with LLM_PROVIDER=fake."""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from examples.hello_agent.agent import run
from fde_kit.connectors.sql import SQLConnector, SQLSettings
from fde_kit.observability import trace_run
from fde_kit.review_queue import ReviewQueue

TICKETS = [
    (1, "I was charged twice on my last invoice, please refund one."),
    (2, "Our dashboard is down, every page returns a 500 error."),
    (3, "Can you tell me what your office hours are?"),
]


def main() -> int:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE tickets (id INTEGER, body TEXT)"))
        for i, b in TICKETS:
            c.execute(text("INSERT INTO tickets VALUES (:i, :b)"), {"i": i, "b": b})
    source = SQLConnector(
        SQLSettings(query="SELECT id, body FROM tickets", id_column="id"), engine=engine
    )
    queue = ReviewQueue("sqlite://")
    for rec in source.fetch():
        with trace_run(f"ticket:{rec.id}") as ctx:
            out = run({"text": rec.payload["body"]}, queue=queue)
        print(
            f"#{rec.id} trace={ctx.trace_id} {out['category']}/{out['priority']} "
            f"-> {out['action']} (${ctx.usage.cost_usd:.6f})"
        )
    print(f"pending human reviews: {len(queue.list(status='pending'))}")
    return 0
