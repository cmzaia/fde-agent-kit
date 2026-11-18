"""Webhook receiver: FastAPI router that verifies an HMAC signature and buffers events."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import uuid
from collections import deque
from collections.abc import Iterator
from typing import Any, Self

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic_settings import BaseSettings, SettingsConfigDict

from fde_kit.connectors.base import Connector, Health, Record, register


class WebhookSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FDE_WEBHOOK_", env_file=".env", extra="ignore")

    secret: str | None = None
    max_queue: int = 10_000


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@register
class WebhookConnector(Connector):
    name = "webhook"

    def __init__(self, settings: WebhookSettings | None = None):
        self.settings = settings or WebhookSettings()
        self._queue: deque[Record] = deque(maxlen=self.settings.max_queue)
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> Self:
        return cls()

    def receive(self, payload: dict[str, Any], event_id: str | None = None) -> Record:
        rec = Record(source=self.name, id=event_id or uuid.uuid4().hex, payload=payload)
        with self._lock:
            self._queue.append(rec)
        return rec

    def fetch(self) -> Iterator[Record]:
        """Drain buffered events (consumed once)."""
        while True:
            with self._lock:
                if not self._queue:
                    return
                rec = self._queue.popleft()
            yield rec

    def verify(self, body: bytes, signature: str | None) -> bool:
        if not self.settings.secret:
            return True  # unsigned mode: only acceptable behind a private network
        return bool(signature) and hmac.compare_digest(
            sign(self.settings.secret, body), signature or ""
        )

    def health(self) -> Health:
        return Health(ok=True, detail=f"{len(self._queue)} buffered")


def create_webhook_router(connector: WebhookConnector) -> APIRouter:
    router = APIRouter()

    @router.post("", status_code=202)
    async def receive(request: Request) -> dict[str, str]:
        body = await request.body()
        if not connector.verify(body, request.headers.get("X-Signature")):
            raise HTTPException(401, "bad signature")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(400, "body must be JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "body must be a JSON object")
        rec = connector.receive(payload, request.headers.get("X-Event-Id"))
        return {"id": rec.id}

    return router


def create_webhook_app(connector: WebhookConnector | None = None) -> FastAPI:
    connector = connector or WebhookConnector.from_env()
    app = FastAPI(title="fde-kit webhook")
    app.include_router(create_webhook_router(connector), prefix="/webhook")
    return app
