"""Манифест источников: CSV, ключ строки (адрес, SHA-256, фонд).

Строка появляется один раз, когда содержимое увидено впервые. Поэтому прогон без изменений
на сайтах оставляет файл байт в байт прежним.
"""

from __future__ import annotations

import csv
import threading
from dataclasses import asdict, fields
from pathlib import Path

from pif.models import SourceRecord

COLUMNS = [f.name for f in fields(SourceRecord)]


class Manifest:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._rows: dict[tuple[str, str, str], SourceRecord] = {}
        self._by_source: dict[str, SourceRecord] = {}
        self._lock = threading.Lock()
        if path.exists():
            with path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    row["size"] = int(row["size"])  # type: ignore[assignment]
                    row["http_status"] = int(row["http_status"])  # type: ignore[assignment]
                    self._remember(SourceRecord(**row))  # type: ignore[arg-type]

    def _remember(self, record: SourceRecord) -> None:
        self._rows[(record.url, record.sha256, record.fund)] = record
        self._by_source.setdefault(record.source_id, record)

    def add(self, record: SourceRecord) -> None:
        with self._lock:
            key = (record.url, record.sha256, record.fund)
            if key not in self._rows:
                self._remember(record)

    def get(self, source_id: str) -> SourceRecord | None:
        return self._by_source.get(source_id)

    def records(self) -> list[SourceRecord]:
        return sorted(self._rows.values(), key=lambda r: (r.uk_inn, r.fund, r.url, r.fetched_at, r.sha256))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(asdict(r) for r in self.records())
        tmp.replace(self.path)
