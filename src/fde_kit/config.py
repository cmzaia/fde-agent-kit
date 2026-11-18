"""Central settings. Every knob is an environment variable (or a line in .env)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODELS = {
    "fake": "fake-triage-1",
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-4o-mini",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_provider: str = "fake"
    llm_model: str | None = None
    llm_temperature: float = 0.0
    llm_max_retries: int = 3
    # Optional override of the illustrative price table (USD per million tokens).
    llm_price_in_per_mtok: float | None = None
    llm_price_out_per_mtok: float | None = None

    log_level: str = "INFO"
    review_db_url: str = "sqlite:///review_queue.db"
    fde_review_api_key: str | None = None

    @property
    def model_name(self) -> str:
        return self.llm_model or DEFAULT_MODELS.get(self.llm_provider.lower(), "unknown")


@lru_cache
def get_settings() -> Settings:
    return Settings()
