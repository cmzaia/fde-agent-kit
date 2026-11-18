"""SQLite/Postgres-backed human-in-the-loop queue: agents enqueue, people approve or reject."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    insert,
    select,
    update,
)
from sqlalchemy.pool import StaticPool

metadata = MetaData()
review_items = Table(
    "review_items",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trace_id", String(64)),
    Column("kind", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("status", String(16), nullable=False, default="pending", index=True),
    Column("reviewer", String(128)),
    Column("note", Text),
    Column("created_at", DateTime, nullable=False),
    Column("decided_at", DateTime),
)

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"


class AlreadyDecidedError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url.rstrip("/") == "sqlite:":
            kwargs["poolclass"] = StaticPool
        return create_engine(url, **kwargs)
    return create_engine(url, pool_pre_ping=True)


class ReviewQueue:
    def __init__(self, url: str = "sqlite:///review_queue.db", engine: Engine | None = None):
        self.engine = engine or make_engine(url)
        metadata.create_all(self.engine)

    def enqueue(self, kind: str, payload: dict[str, Any], trace_id: str | None = None) -> int:
        with self.engine.begin() as conn:
            res = conn.execute(
                insert(review_items).values(
                    kind=kind, payload=payload, trace_id=trace_id, status=PENDING, created_at=_now()
                )
            )
            return int(res.inserted_primary_key[0])

    def get(self, item_id: int) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(review_items).where(review_items.c.id == item_id))
                .mappings()
                .first()
            )
        return _serialize(row) if row else None

    def list(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        q = select(review_items).order_by(review_items.c.id).limit(limit)
        if status:
            q = q.where(review_items.c.status == status)
        with self.engine.connect() as conn:
            return [_serialize(r) for r in conn.execute(q).mappings()]

    def decide(
        self, item_id: int, status: str, reviewer: str | None = None, note: str | None = None
    ) -> dict[str, Any]:
        """Atomically move a pending item to approved/rejected. Raises if already decided."""
        if status not in {APPROVED, REJECTED}:
            raise ValueError(status)
        with self.engine.begin() as conn:
            res = conn.execute(
                update(review_items)
                .where(review_items.c.id == item_id, review_items.c.status == PENDING)
                .values(status=status, reviewer=reviewer, note=note, decided_at=_now())
            )
            if res.rowcount == 0:
                exists = conn.execute(
                    select(review_items.c.id).where(review_items.c.id == item_id)
                ).first()
                if exists is None:
                    raise KeyError(item_id)
                raise AlreadyDecidedError(item_id)
        item = self.get(item_id)
        assert item is not None
        return item


def _serialize(row: Any) -> dict[str, Any]:
    d = dict(row)
    for k in ("created_at", "decided_at"):
        d[k] = d[k].isoformat() if d[k] else None
    return d
