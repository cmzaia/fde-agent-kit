"""One FastAPI app for the deployed service: health, webhook intake, human review."""

from __future__ import annotations

from fastapi import FastAPI

from fde_kit.config import Settings, get_settings
from fde_kit.connectors.webhook import WebhookConnector, create_webhook_router
from fde_kit.observability import active_integrations, configure_logging
from fde_kit.review_queue import ReviewQueue, create_review_router


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    configure_logging(s.log_level)
    queue = ReviewQueue(s.review_db_url)
    webhook = WebhookConnector.from_env()
    app = FastAPI(title="fde-kit service")
    app.state.queue, app.state.webhook = queue, webhook
    app.include_router(create_webhook_router(webhook), prefix="/webhook")
    app.include_router(create_review_router(queue, s.fde_review_api_key), prefix="/review")

    @app.get("/healthz")
    def healthz() -> dict:
        return {
            "status": "ok",
            "provider": s.llm_provider,
            "model": s.model_name,
            "tracing": active_integrations(),
        }

    return app
