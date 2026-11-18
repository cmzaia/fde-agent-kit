"""SQL connector (SQLAlchemy): runs one configured read-only query against Postgres or SQLite."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Self

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, create_engine, text

from fde_kit.connectors.base import Connector, Health, Record, register


class SQLSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FDE_SQL_", env_file=".env", extra="ignore")

    url: str = "sqlite:///:memory:"
    query: str = "SELECT 1 AS ok"
    id_column: str | None = None
    max_rows: int = 1000


def validate_readonly(query: str) -> str:
    """Belt and braces: the DB role should be read-only too, but never run anything but SELECT."""
    q = query.strip().rstrip(";")
    if not re.match(r"^(select|with)\b", q, re.I) or ";" in q:
        raise ValueError("FDE_SQL_QUERY must be a single SELECT/WITH statement")
    if re.search(r"\b(insert|update|delete|drop|alter|truncate|create|grant)\b", q, re.I):
        raise ValueError("FDE_SQL_QUERY contains a write/DDL keyword")
    return q


@register
class SQLConnector(Connector):
    name = "sql"

    def __init__(self, settings: SQLSettings | None = None, engine: Engine | None = None):
        self.settings = settings or SQLSettings()
        self.query = validate_readonly(self.settings.query)
        self.engine = engine or create_engine(self.settings.url, pool_pre_ping=True)

    @classmethod
    def from_env(cls) -> Self:
        return cls()

    def fetch(self) -> Iterator[Record]:
        with self.engine.connect() as conn:
            result = conn.execute(text(self.query))
            for i, row in enumerate(result.mappings()):
                if i >= self.settings.max_rows:
                    break
                data = dict(row)
                rid = (
                    str(data.get(self.settings.id_column, i)) if self.settings.id_column else str(i)
                )
                yield Record(source=self.name, id=rid, payload=data)

    def health(self) -> Health:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return Health(ok=True)
        except Exception as e:
            return Health(ok=False, detail=str(e)[:200])

    def close(self) -> None:
        self.engine.dispose()
