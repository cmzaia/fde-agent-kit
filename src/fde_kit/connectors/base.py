"""The Connector contract: pull normalized Records out of a customer system."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar, Self

from pydantic import BaseModel, Field


class Record(BaseModel):
    """A normalized unit of work, whatever system it came from."""

    source: str
    id: str
    payload: dict[str, Any]
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Health(BaseModel):
    ok: bool
    detail: str = ""


class Connector(ABC):
    """Implement `fetch` (and `from_env`) to add a new data source.

    Rules of the road: config comes from env via a pydantic-settings class, `fetch` is a
    generator (so the agent can stream), and failures raise instead of returning empty.
    """

    name: ClassVar[str]

    @classmethod
    @abstractmethod
    def from_env(cls) -> Self:
        """Build the connector from environment variables."""

    @abstractmethod
    def fetch(self) -> Iterator[Record]:
        """Yield new records. Must be safe to call repeatedly."""

    def health(self) -> Health:
        return Health(ok=True, detail="no check implemented")

    def close(self) -> None:
        """Release connections. Default: nothing to release."""
        return None


_REGISTRY: dict[str, type[Connector]] = {}


def register[C: type[Connector]](cls: C) -> C:
    """Class decorator: make a connector available to `build_connector(name)`."""
    _REGISTRY[cls.name] = cls
    return cls


def available_connectors() -> list[str]:
    return sorted(_REGISTRY)


def build_connector(name: str) -> Connector:
    try:
        cls = _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown connector {name!r}; available: {available_connectors()}") from None
    return cls.from_env()
