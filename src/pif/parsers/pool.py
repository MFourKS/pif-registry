"""Разбор документов вне цикла событий.

Разбор PDF занимает доли секунды процессорного времени. Внутри asyncio это остановило бы все
сетевые задачи, поэтому документы разбираются в пуле процессов. Результат кэшируется по SHA-256:
КИД не меняется, и повторный прогон ничего не разбирает заново.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from pif.errors import NotApplicableDocument, ParseError, ScannedDocument, UnknownFormatError
from pif.parsers.kid import KidData, parse_kid
from pif.parsers.nav_statement import NavStatement, parse_nav_statement
from pif.storage.atomic import replace_file

PARSER_VERSION = 11


def _kid_to_json(kid: KidData) -> dict[str, Any]:
    data = asdict(kid)
    data["report_date"] = kid.report_date.isoformat()
    data["unit_price"] = None if kid.unit_price is None else str(kid.unit_price)
    data["nav"] = None if kid.nav is None else str(kid.nav)
    data["returns"] = {str(k): str(v) for k, v in kid.returns.items()}
    return data


def _kid_from_json(data: dict[str, Any]) -> KidData:
    return KidData(
        report_date=date.fromisoformat(data["report_date"]),
        fund_name=data["fund_name"],
        unit_price=Decimal(data["unit_price"]) if data["unit_price"] else None,
        nav=Decimal(data["nav"]) if data["nav"] else None,
        currency=data["currency"],
        returns={int(k): Decimal(v) for k, v in data["returns"].items()},
        pays_income=data.get("pays_income"),
    )


def _quiet_worker() -> None:
    """Предупреждения pdfminer о шрифтах бесполезны и засоряют вывод прогона."""
    logging.disable(logging.WARNING)


def _safe_parse_kid(content: bytes, fmt: str, source: str) -> tuple[str, Any]:
    try:
        return "ok", _kid_to_json(parse_kid(content, fmt, source))
    except NotApplicableDocument as exc:
        return "skip", str(exc)
    except ScannedDocument as exc:
        return "scan", str(exc)
    except ParseError as exc:
        return "error", str(exc)


def _safe_parse_statement(content: bytes, fmt: str, source: str) -> tuple[str, Any]:
    try:
        return "ok", parse_nav_statement(content, fmt, source)
    except ParseError as exc:
        return "error", str(exc)
    except Exception as exc:
        return "error", f"{type(exc).__name__}: {exc}"


class DocumentParser:
    def __init__(self, cache_path: Path, workers: int | None = None) -> None:
        self.cache_path = cache_path
        self._pool = ProcessPoolExecutor(
            max_workers=workers or max(2, (os.cpu_count() or 4) - 2), initializer=_quiet_worker
        )
        self._cache: dict[str, Any] = {}
        if cache_path.exists():
            stored = json.loads(cache_path.read_text(encoding="utf-8"))
            if stored.get("version") == PARSER_VERSION:
                self._cache = stored["items"]

    async def kid(self, sha256: str, content: bytes, fmt: str, source: str) -> KidData:
        hit: Any = self._cache.get(sha256)
        if hit is None:
            loop = asyncio.get_running_loop()
            hit = await loop.run_in_executor(self._pool, _safe_parse_kid, content, fmt, source)
            self._cache[sha256] = hit
        status, payload = hit
        if status == "skip":
            raise NotApplicableDocument(payload)
        if status == "scan":
            raise ScannedDocument(payload)
        if status == "error":
            raise UnknownFormatError(source, payload)
        return _kid_from_json(payload)

    async def statement(self, content: bytes, fmt: str, source: str) -> NavStatement:
        loop = asyncio.get_running_loop()
        status, payload = await loop.run_in_executor(self._pool, _safe_parse_statement, content, fmt, source)
        if status == "error":
            raise UnknownFormatError(source, payload)
        return payload

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": PARSER_VERSION, "items": self._cache}, ensure_ascii=False), encoding="utf-8")
        replace_file(tmp, self.cache_path)

    def close(self) -> None:
        self.save()
        self._pool.shutdown(cancel_futures=True)
