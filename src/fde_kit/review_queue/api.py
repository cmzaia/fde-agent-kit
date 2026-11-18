"""FastAPI endpoints for the review queue: list, create, approve, reject."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from fde_kit.review_queue.store import APPROVED, REJECTED, AlreadyDecidedError, ReviewQueue


class NewItem(BaseModel):
    kind: str
    payload: dict[str, Any]
    trace_id: str | None = None


class Decision(BaseModel):
    reviewer: str | None = None
    note: str | None = None


def create_review_router(queue: ReviewQueue, api_key: str | None = None) -> APIRouter:
    def auth(x_api_key: str | None = Header(default=None)) -> None:
        if api_key and not hmac.compare_digest(x_api_key or "", api_key):
            raise HTTPException(401, "missing or bad X-API-Key")

    router = APIRouter(dependencies=[Depends(auth)])

    @router.get("/items")
    def list_items(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return queue.list(status=status, limit=limit)

    @router.post("/items", status_code=201)
    def create_item(item: NewItem) -> dict[str, Any]:
        item_id = queue.enqueue(item.kind, item.payload, item.trace_id)
        return queue.get(item_id) or {}

    @router.get("/items/{item_id}")
    def get_item(item_id: int) -> dict[str, Any]:
        item = queue.get(item_id)
        if item is None:
            raise HTTPException(404, "no such item")
        return item

    def _decide(item_id: int, status: str, d: Decision) -> dict[str, Any]:
        try:
            return queue.decide(item_id, status, d.reviewer, d.note)
        except KeyError:
            raise HTTPException(404, "no such item") from None
        except AlreadyDecidedError:
            raise HTTPException(409, "item already decided") from None

    @router.post("/items/{item_id}/approve")
    def approve(item_id: int, d: Decision | None = None) -> dict[str, Any]:
        return _decide(item_id, APPROVED, d or Decision())

    @router.post("/items/{item_id}/reject")
    def reject(item_id: int, d: Decision | None = None) -> dict[str, Any]:
        return _decide(item_id, REJECTED, d or Decision())

    return router
