"""IMAP mailbox reader. Read-only (BODY.PEEK), so it never flips the customer's unread flags."""

from __future__ import annotations

import imaplib
from collections.abc import Callable, Iterator
from email import message_from_bytes, policy
from typing import Any, Self

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fde_kit.connectors.base import Connector, Health, Record, register


class IMAPSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FDE_IMAP_", env_file=".env", extra="ignore")

    host: str = "localhost"
    port: int = 993
    username: str = ""
    password: SecretStr = SecretStr("")
    mailbox: str = "INBOX"
    search: str = "UNSEEN"
    max_messages: int = 50


def parse_message(raw: bytes, fallback_id: str) -> Record:
    msg = message_from_bytes(raw, policy=policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    attachments = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    return Record(
        source="imap",
        id=str(msg["Message-ID"] or fallback_id),
        payload={
            "subject": str(msg["Subject"] or ""),
            "from": str(msg["From"] or ""),
            "to": str(msg["To"] or ""),
            "date": str(msg["Date"] or ""),
            "body": body.get_content().strip() if body else "",
            "attachments": attachments,
        },
    )


@register
class IMAPConnector(Connector):
    name = "imap"

    def __init__(
        self, settings: IMAPSettings | None = None, client_factory: Callable[[], Any] | None = None
    ):
        self.settings = settings or IMAPSettings()
        # Injectable so tests (and odd customer servers, e.g. STARTTLS) can swap the client.
        self._factory = client_factory or (
            lambda: imaplib.IMAP4_SSL(self.settings.host, self.settings.port)
        )

    @classmethod
    def from_env(cls) -> Self:
        return cls()

    def _connect(self) -> Any:
        client = self._factory()
        client.login(self.settings.username, self.settings.password.get_secret_value())
        client.select(self.settings.mailbox, readonly=True)
        return client

    def fetch(self) -> Iterator[Record]:
        client = self._connect()
        try:
            _, data = client.search(None, self.settings.search)
            ids = data[0].split()[: self.settings.max_messages]
            for mid in ids:
                _, parts = client.fetch(mid, "(BODY.PEEK[])")
                yield parse_message(parts[0][1], fallback_id=mid.decode())
        finally:
            client.logout()

    def health(self) -> Health:
        try:
            self._connect().logout()
            return Health(ok=True)
        except Exception as e:
            return Health(ok=False, detail=str(e)[:200])
