"""Вежливый асинхронный HTTP-клиент.

Гарантии:
* не чаще одного запроса в секунду на хост, независимо от числа параллельных задач;
* честный User-Agent, соблюдение robots.txt, капчи и 403 не обходятся;
* условные запросы (ETag, Last-Modified) и окно свежести: повторный запуск не качает заново;
* каждый ответ сохраняется в сыром виде и записывается в манифест.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from opentelemetry.trace import Status, StatusCode

from pif.config import Settings
from pif.errors import AccessBlocked, FetchError, ResourceNotFound, RobotsDisallowed, SiteUnavailable
from pif.models import SourceRecord
from pif.net.robots import RobotsRules
from pif.observability import get_logger, tracer
from pif.storage.atomic import replace_file
from pif.storage.manifest import Manifest
from pif.storage.rawstore import RawStore, sha256_of, source_id_of

log = get_logger("net")

BOT_TOKEN = "pif-registry"
CACHE_SAVE_EVERY = 200

_CAPTCHA_MARKERS = re.compile(
    rb"(?i)(g-recaptcha|smartcaptcha|hcaptcha|cf-chl-|ddos-guard|checking your browser|"
    rb"are you (a )?human|servicepipe|qrator|captcha_form)"
)
_FORMATS = {
    "text/html": "html",
    "application/json": "json",
    "text/csv": "csv",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "text/plain": "txt",
    "text/xml": "xml",
    "application/xml": "xml",
}


def detect_format(content: bytes, content_type: str, url: str) -> str:
    """Формат определяется по содержимому: заголовкам и расширениям сайтов верить нельзя."""
    head = content[:8].lstrip()
    if head.startswith(b"%PDF"):
        return "pdf"
    if content[:2] == b"PK":
        try:
            import io
            import zipfile

            names = zipfile.ZipFile(io.BytesIO(content)).namelist()
        except Exception:
            return "zip"
        if any(n.startswith("word/") for n in names):
            return "docx"
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        return "zip"
    if content[:4] == b"\xd0\xcf\x11\xe0":
        return "doc" if url.lower().split("?")[0].endswith((".doc", ".rtf")) else "xls"
    if content[:5] == b"{\\rtf":
        return "rtf"
    mime = content_type.split(";", maxsplit=1)[0].strip().lower()
    if mime in _FORMATS:
        return _FORMATS[mime]
    stripped = content[:200].lstrip().lower()
    if stripped.startswith((b"<!doctype", b"<html")):
        return "html"
    if stripped[:1] in (b"{", b"["):
        return "json"
    suffix = urlsplit(url).path.rsplit(".", 1)[-1].lower()
    return suffix if suffix in {"csv", "txt", "xml"} else "bin"


@dataclass(slots=True)
class FetchResult:
    url: str
    final_url: str
    content: bytes
    fmt: str
    sha256: str
    source_id: str
    http_status: int
    from_cache: bool
    content_type: str = ""

    @property
    def text(self) -> str:
        return decode_text(self.content, self.content_type)


def decode_text(content: bytes, content_type: str = "") -> str:
    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    candidates = [match.group(1)] if match else []
    meta = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", content[:4096], re.I)
    if meta:
        candidates.append(meta.group(1).decode("ascii", "ignore"))
    for encoding in [*candidates, "utf-8", "cp1251"]:
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", "replace")


@dataclass(slots=True)
class _HostGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_slot: float = 0.0


_FAILURE_KINDS: dict[str, type[FetchError]] = {
    kind.__name__: kind for kind in (FetchError, SiteUnavailable, AccessBlocked, ResourceNotFound, RobotsDisallowed)
}


@dataclass(slots=True)
class FetchStats:
    sent: int = 0
    network: int = 0
    not_modified: int = 0
    fresh_cache: int = 0
    failures_from_cache: int = 0
    errors: int = 0
    robots_denied: int = 0
    robots_public_documents: int = 0
    bytes_downloaded: int = 0
    tls_unverified_hosts: set[str] = field(default_factory=set)


class HttpCache:
    """Служебное состояние: когда и с каким ETag запрашивали адрес и какие адреса не ответили.

    В манифест не попадает. Ошибки хранятся отдельно: повторный запуск должен получить тот же отказ,
    а не спрашивать сайт снова и получать другой текст ошибки.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.failures_path = path.with_name("http_failures.json")
        self.entries: dict[str, dict[str, Any]] = self._load(path)
        self.failures: dict[str, dict[str, Any]] = self._load(self.failures_path)

    @staticmethod
    def _load(path: Path) -> dict[str, dict[str, Any]]:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for path, payload in ((self.path, self.entries), (self.failures_path, self.failures)):
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            replace_file(tmp, path)


class Fetcher:
    def __init__(
        self,
        settings: Settings,
        raw: RawStore,
        manifest: Manifest,
        *,
        force: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.raw = raw
        self.manifest = manifest
        self.force = force
        self.cache = HttpCache(settings.state_dir / "http_cache.json")
        self.stats = FetchStats()
        self._gates: dict[str, _HostGate] = {}
        self._robots: dict[str, RobotsRules | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        common: dict[str, Any] = {
            "timeout": httpx.Timeout(settings.request_timeout_s),
            "follow_redirects": True,
            "headers": {"User-Agent": settings.user_agent, "Accept-Language": "ru"},
            "trust_env": False,
            "limits": httpx.Limits(max_connections=64, max_keepalive_connections=16),
            "transport": transport,
        }
        self._client = httpx.AsyncClient(**common)
        # Часть сайтов УК работает на сертификатах НУЦ Минцифры, которых нет в certifi.
        # Для таких хостов откатываемся на запрос без проверки цепочки и пишем это в манифест.
        self._client_noverify = httpx.AsyncClient(verify=False, **common)

    async def aclose(self) -> None:
        self.cache.save()
        await self._client.aclose()
        await self._client_noverify.aclose()

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def _client_for(self, host: str) -> httpx.AsyncClient:
        return self._client_noverify if host in self.stats.tls_unverified_hosts else self._client

    async def _wait_slot(self, host: str) -> None:
        # www.site.ru и site.ru — один сайт, лимит у них общий.
        gate = self._gates.setdefault(host.removeprefix("www."), _HostGate())
        async with gate.lock:
            now = time.monotonic()
            wait = gate.next_slot - now
            gate.next_slot = max(now, gate.next_slot) + self.settings.min_host_interval_s
        if wait > 0:
            await asyncio.sleep(wait)

    async def _send(self, method: str, url: str, host: str, **kwargs: Any) -> httpx.Response:
        await self._wait_slot(host)
        self.stats.sent += 1
        try:
            return await self._client_for(host).request(method, url, **kwargs)
        except httpx.ConnectError as exc:
            if "CERTIFICATE_VERIFY_FAILED" not in str(exc) or host in self.stats.tls_unverified_hosts:
                raise
            self.stats.tls_unverified_hosts.add(host)
            log.warning("tls_fallback_unverified", host=host)
            await self._wait_slot(host)
            return await self._client_noverify.request(method, url, **kwargs)

    async def _load_robots(self, scheme: str, host: str) -> RobotsRules | None:
        key = f"robots:{host}"
        known = self.cache.entries.get(key, {})
        remembered = known.get("text")
        if not self.force and time.time() - known.get("checked_at", 0) < self.settings.freshness_ttl_s:
            return RobotsRules.parse(remembered, BOT_TOKEN) if remembered and not known.get("absent") else None
        try:
            resp = await self._send("GET", f"{scheme}://{host}/robots.txt", host)
        except httpx.HTTPError as exc:
            # Сбой сети не отменяет запрет: берём правила, которые видели в прошлый раз.
            log.info("robots_unavailable", host=host, error=type(exc).__name__)
            self.cache.entries[key] = {**known, "checked_at": time.time()}
            return RobotsRules.parse(remembered, BOT_TOKEN) if remembered and not known.get("absent") else None
        if resp.status_code == 200 and "html" not in resp.headers.get("content-type", ""):
            self.cache.entries[key] = {"text": resp.text, "checked_at": time.time()}
            return RobotsRules.parse(resp.text, BOT_TOKEN)
        self.cache.entries[key] = {"text": remembered, "absent": True, "checked_at": time.time()}
        return None

    async def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = parts.netloc
        lock = self._robots_locks.setdefault(host, asyncio.Lock())
        async with lock:
            if host not in self._robots:
                self._robots[host] = await self._load_robots(parts.scheme, host)
        rules = self._robots[host]
        return rules is None or rules.allowed(url)

    async def require_open_site(self, url: str) -> None:
        """Сайт с `Disallow: /` для всех закрыт целиком, в том числе для ответов из кэша."""
        parts = urlsplit(url)
        if not await self._allowed(f"{parts.scheme}://{parts.netloc}/"):
            raise RobotsDisallowed(url, "сайт целиком закрыт для роботов в robots.txt")

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        as_json: bool = False,
        headers: dict[str, str] | None = None,
        uk_inn: str = "",
        fund: str = "",
        ttl: int | None = None,
        retries: int | None = None,
        documented_api: bool = False,
        public_document: bool = False,
    ) -> FetchResult:
        """Скачивает ресурс с соблюдением robots.txt. Исключений два, оба лимит запросов сохраняют.

        documented_api: публичный API с документацией. На iss.moex.com robots закрывает всё от
        поисковиков, хотя сам ISS создан для программного доступа.
        public_document: файл раскрытия по прямой ссылке с открытой страницы. Шаблонный
        `Disallow: /upload/` из Битрикса защищает выдачу поисковиков, а обязательные к публикации
        документы лежат именно там. Такой запрос помечается в манифесте. PIF_STRICT_ROBOTS=1
        отключает это исключение.
        """
        request = httpx.Request(method, url, params=params)
        full_url = str(request.url)
        key = full_url if data is None else f"{full_url}#{json.dumps(data, sort_keys=True, ensure_ascii=False)}"
        host = request.url.host if not request.url.port else f"{request.url.host}:{request.url.port}"
        entry = self.cache.entries.get(key)
        ttl = self.settings.freshness_ttl_s if ttl is None else ttl

        if entry and not self.force and time.time() - entry["checked_at"] < ttl:
            body = self.raw.get(entry["sha256"], entry["ext"])
            if body is not None:
                self.stats.fresh_cache += 1
                return self._result(full_url, entry, body, from_cache=True)

        # Долгий срок КИД относится к файлу, а отказ сайта помним только на обычное окно свежести.
        failure = self.cache.failures.get(key)
        if failure and not self.force and time.time() - failure["checked_at"] < min(ttl, self.settings.freshness_ttl_s):
            self.stats.failures_from_cache += 1
            raise _FAILURE_KINDS[failure["kind"]](full_url, failure["detail"])

        try:
            result = await self._request(
                method, full_url, key, host, entry, headers, data, as_json, uk_inn, fund, retries, documented_api,
                public_document,
            )
        except FetchError as exc:
            self.cache.failures[key] = {"kind": type(exc).__name__, "detail": exc.detail, "checked_at": time.time()}
            raise
        self.cache.failures.pop(key, None)
        return result

    async def _request(
        self,
        method: str,
        full_url: str,
        key: str,
        host: str,
        entry: dict[str, Any] | None,
        headers: dict[str, str] | None,
        data: dict[str, Any] | None,
        as_json: bool,
        uk_inn: str,
        fund: str,
        retries: int | None,
        documented_api: bool,
        public_document: bool,
    ) -> FetchResult:
        with tracer().start_as_current_span("http.fetch") as span:
            span.set_attribute("url.full", full_url)
            span.set_attribute("server.address", host)
            allowed = documented_api or await self._allowed(full_url)
            robots_note = ""
            if not allowed and public_document and not self.settings.strict_robots:
                allowed, robots_note = True, "robots_disallowed_public_document"
                self.stats.robots_public_documents += 1
            if not allowed:
                self.stats.robots_denied += 1
                span.set_attribute("pif.robots_denied", True)
                raise RobotsDisallowed(full_url, "адрес закрыт в robots.txt")

            conditional: dict[str, str] = dict(headers or {})
            cached_body = self.raw.get(entry["sha256"], entry["ext"]) if entry else None
            if entry and cached_body is not None and not self.force:
                if entry.get("etag"):
                    conditional["If-None-Match"] = entry["etag"]
                if entry.get("last_modified"):
                    conditional["If-Modified-Since"] = entry["last_modified"]

            attempts = (self.settings.retries if retries is None else retries) + 1
            last_error: FetchError | None = None
            for attempt in range(attempts):
                try:
                    body = {"json": data} if as_json else {"data": data}
                    response = await self._send(method, full_url, host, headers=conditional, **body)
                except httpx.HTTPError as exc:
                    last_error = SiteUnavailable(full_url, f"{type(exc).__name__}: {exc}"[:200])
                else:
                    if response.status_code < 500 and response.status_code != 429:
                        span.set_attribute("http.response.status_code", response.status_code)
                        return self._accept(key, full_url, response, entry, cached_body, uk_inn, fund, robots_note)
                    last_error = (
                        AccessBlocked(full_url, "HTTP 429")
                        if response.status_code == 429
                        else SiteUnavailable(full_url, f"HTTP {response.status_code}")
                    )
                if attempt + 1 < attempts:
                    await asyncio.sleep(2.0 * 2**attempt)
            assert last_error is not None
            self.stats.errors += 1
            span.set_status(Status(StatusCode.ERROR, str(last_error)))
            raise last_error

    def _accept(
        self,
        key: str,
        full_url: str,
        response: httpx.Response,
        entry: dict[str, Any] | None,
        cached_body: bytes | None,
        uk_inn: str,
        fund: str,
        note: str = "",
    ) -> FetchResult:
        if response.status_code == 304 and entry and cached_body is not None:
            self.stats.not_modified += 1
            entry["checked_at"] = time.time()
            return self._result(full_url, entry, cached_body, from_cache=True)

        content = response.content
        if response.status_code in (401, 403, 451) or (
            response.status_code == 200 and len(content) < 60_000 and _CAPTCHA_MARKERS.search(content)
        ):
            self.stats.errors += 1
            raise AccessBlocked(full_url, f"HTTP {response.status_code}, доступ закрыт или капча")
        if response.status_code in {404, 410}:
            raise ResourceNotFound(full_url, f"HTTP {response.status_code}")
        if response.status_code >= 400:
            self.stats.errors += 1
            raise FetchError(full_url, f"HTTP {response.status_code}")

        self.stats.network += 1
        self.stats.bytes_downloaded += len(content)
        content_type = response.headers.get("content-type", "")
        fmt = detect_format(content, content_type, full_url)
        sha, _ = self.raw.put(content, fmt)
        new_entry = {
            "sha256": sha,
            "ext": fmt,
            "fmt": fmt,
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
            "content_type": content_type,
            "final_url": str(response.url),
            "http_status": response.status_code,
            "checked_at": time.time(),
        }
        self.cache.entries[key] = new_entry
        if self.stats.network % CACHE_SAVE_EVERY == 0:
            # Длинный обход одной УК не должен терять скачанное при обрыве.
            self.cache.save()
        host = urlsplit(full_url).netloc
        self.manifest.add(
            SourceRecord(
                source_id=source_id_of(sha),
                url=full_url,
                fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
                fmt=fmt,
                sha256=sha,
                size=len(content),
                http_status=response.status_code,
                status="ok",
                uk_inn=uk_inn,
                fund=fund,
                note=" ".join(filter(None, [note, "tls_unverified" if host in self.stats.tls_unverified_hosts else ""])),
            )
        )
        return self._result(full_url, new_entry, content, from_cache=False)

    def manifest_link(self, source: FetchResult, uk_inn: str, fund: str) -> None:
        """Привязывает уже скачанный ресурс к фонду: страница одна, фондов на ней может быть много."""
        known = self.manifest.get(source.source_id)
        if known is None:
            return
        self.manifest.add(replace(known, url=source.url, uk_inn=uk_inn or known.uk_inn, fund=fund))

    def _remember_in_manifest(self, url: str, entry: dict[str, Any], body: bytes) -> None:
        """Ответ из кэша тоже должен быть в манифесте: иначе у точки не будет адреса источника."""
        source_id = source_id_of(entry["sha256"])
        if self.manifest.get(source_id) is not None:
            return
        fetched = datetime.fromtimestamp(entry["checked_at"], UTC).isoformat(timespec="seconds")
        self.manifest.add(
            SourceRecord(
                source_id, url, fetched, entry["fmt"], entry["sha256"], len(body),
                entry.get("http_status", 200), "ok",
            )
        )  # fmt: skip

    def _result(self, url: str, entry: dict[str, Any], body: bytes, *, from_cache: bool) -> FetchResult:
        if from_cache:
            self._remember_in_manifest(url, entry, body)
        return FetchResult(
            url=url,
            final_url=entry.get("final_url", url),
            content=body,
            fmt=entry["fmt"],
            sha256=entry["sha256"],
            source_id=source_id_of(entry["sha256"]),
            http_status=entry.get("http_status", 200),
            from_cache=from_cache,
            content_type=entry.get("content_type", ""),
        )


__all__ = ["FetchResult", "Fetcher", "FetchStats", "decode_text", "detect_format", "sha256_of"]
