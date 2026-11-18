"""Local drop-folder watcher: CSV rows and PDF text from files a customer drops in a directory."""

from __future__ import annotations

import csv
import shutil
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Self

from pydantic_settings import BaseSettings, SettingsConfigDict

from fde_kit.connectors.base import Connector, Health, Record, register


class DropFolderSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FDE_DROP_", env_file=".env", extra="ignore")

    path: Path = Path("./drop")
    processed_dir: str = "processed"


def read_pdf_text(path: Path) -> tuple[str, int]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join((p.extract_text() or "") for p in reader.pages).strip(), len(reader.pages)


@register
class DropFolderConnector(Connector):
    name = "dropfolder"

    def __init__(self, settings: DropFolderSettings | None = None):
        self.settings = settings or DropFolderSettings()
        self.settings.path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> Self:
        return cls()

    def _records_for(self, f: Path) -> Iterator[Record]:
        if f.suffix.lower() == ".csv":
            with f.open(newline="", encoding="utf-8-sig") as fh:
                for n, row in enumerate(csv.DictReader(fh), start=1):
                    yield Record(
                        source=self.name, id=f"{f.name}:{n}", payload={"file": f.name, **row}
                    )
        elif f.suffix.lower() == ".pdf":
            text, pages = read_pdf_text(f)
            yield Record(
                source=self.name,
                id=f.name,
                payload={"file": f.name, "text": text, "pages": pages},
            )

    def fetch(self) -> Iterator[Record]:
        """Yield records, then move each file to `processed/` (at-least-once delivery)."""
        done = self.settings.path / self.settings.processed_dir
        for f in sorted(p for p in self.settings.path.iterdir() if p.is_file()):
            if f.suffix.lower() not in {".csv", ".pdf"}:
                continue
            yield from self._records_for(f)
            done.mkdir(exist_ok=True)
            shutil.move(str(f), str(done / f.name))

    def watch(
        self,
        handler: Callable[[Record], None],
        interval: float = 2.0,
        max_cycles: int | None = None,
    ) -> None:
        """Poll the folder and hand every record to `handler`. `max_cycles` is for tests."""
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            for rec in self.fetch():
                handler(rec)
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                time.sleep(interval)

    def health(self) -> Health:
        ok = self.settings.path.is_dir()
        return Health(ok=ok, detail="" if ok else f"{self.settings.path} missing")
