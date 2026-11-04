"""HTTP-клиент: лимит запросов, кэш, блокировки, robots и повторный запуск."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import httpx
import pytest

from conftest import observation
from pif.config import Settings
from pif.errors import AccessBlocked, ResourceNotFound, RobotsDisallowed, SiteUnavailable
from pif.models import SourceRecord
from pif.net.client import Fetcher, detect_format
from pif.net.robots import RobotsRules
from pif.storage.manifest import Manifest
from pif.storage.rawstore import RawStore
from pif.storage.series_store import SeriesStore


def settings_for(tmp_path, **overrides) -> Settings:
    base = Settings(
        data_dir=tmp_path / "data",
        raw_dir=tmp_path / "raw",
        state_dir=tmp_path / "state",
        out_dir=tmp_path / "out",
        manifest_path=tmp_path / "manifest.csv",
        min_host_interval_s=0.0,
        retries=1,
    )
    return replace(base, **overrides)


def fetcher_for(tmp_path, handler, **kwargs) -> Fetcher:
    settings = kwargs.pop("settings", settings_for(tmp_path))
    return Fetcher(
        settings,
        RawStore(settings.raw_dir),
        Manifest(settings.manifest_path),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def robots_or(handler):
    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return handler(request)

    return wrapped


async def test_second_run_inside_freshness_window_makes_no_requests(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, text="<html>данные</html>", headers={"content-type": "text/html"})

    async with fetcher_for(tmp_path, robots_or(handler)) as fetcher:
        await fetcher.get("https://uk.example/page")
    async with fetcher_for(tmp_path, robots_or(handler)) as fetcher:
        result = await fetcher.get("https://uk.example/page")
        assert result.from_cache
        assert fetcher.stats.network == 0
    assert calls == ["/page"]


async def test_conditional_request_gets_304_and_keeps_body(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, text="<html>тело</html>", headers={"etag": '"v1"', "content-type": "text/html"})

    settings = settings_for(tmp_path, freshness_ttl_s=0)
    async with fetcher_for(tmp_path, robots_or(handler), settings=settings) as fetcher:
        await fetcher.get("https://uk.example/page")
    async with fetcher_for(tmp_path, robots_or(handler), settings=settings) as fetcher:
        result = await fetcher.get("https://uk.example/page")
        assert fetcher.stats.not_modified == 1
        assert b"\xd1\x82\xd0\xb5\xd0\xbb\xd0\xbe" in result.content
    assert seen == [None, '"v1"']


async def test_manifest_has_url_format_and_hash(tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"%PDF-1.7 test", headers={"content-type": "application/octet-stream"})

    async with fetcher_for(tmp_path, robots_or(handler)) as fetcher:
        result = await fetcher.get("https://uk.example/kid", uk_inn="7700000000", fund="0001")
        fetcher.manifest.save()
    record = Manifest(tmp_path / "manifest.csv").get(result.source_id)
    assert record.url == "https://uk.example/kid"
    assert record.fmt == "pdf"
    assert len(record.sha256) == 64
    assert record.fund == "0001"


def test_manifest_stays_identical_when_nothing_changed(tmp_path):
    path = tmp_path / "m.csv"
    record = SourceRecord("a" * 16, "https://x", "2026-11-04T00:00:00+00:00", "html", "a" * 64, 1, 200, "ok")
    manifest = Manifest(path)
    manifest.add(record)
    manifest.save()
    first = path.read_bytes()
    again = Manifest(path)
    again.add(replace(record, fetched_at="2026-11-05T00:00:00+00:00"))
    again.save()
    assert path.read_bytes() == first


async def test_rate_limit_holds_across_parallel_requests(tmp_path):
    moments = []

    def handler(request):
        moments.append(time.monotonic())
        return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})

    settings = settings_for(tmp_path, min_host_interval_s=0.05)
    async with fetcher_for(tmp_path, robots_or(handler), settings=settings) as fetcher:
        started = time.monotonic()
        await asyncio.gather(*(fetcher.get(f"https://uk.example/{i}") for i in range(5)))
    # Шесть запросов с учётом robots.txt: минимум пять интервалов. Сравниваем общее время,
    # потому что разрыв между соседними запросами зависит от загрузки машины.
    assert time.monotonic() - started >= 0.05 * 5 * 0.95
    assert len(moments) == 5


async def test_different_hosts_do_not_wait_for_each_other(tmp_path):
    def handler(request):
        return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})

    settings = settings_for(tmp_path, min_host_interval_s=0.2)
    async with fetcher_for(tmp_path, robots_or(handler), settings=settings) as fetcher:
        started = time.monotonic()
        await asyncio.gather(*(fetcher.get(f"https://uk{i}.example/") for i in range(5)))
    assert time.monotonic() - started < 0.7


@pytest.mark.parametrize(
    ("status", "error"),
    [(403, AccessBlocked), (404, ResourceNotFound), (503, SiteUnavailable), (429, AccessBlocked)],
)
async def test_http_errors_map_to_honest_statuses(tmp_path, status, error, monkeypatch):
    monkeypatch.setattr("pif.net.client.asyncio.sleep", _no_sleep)

    def handler(request):
        return httpx.Response(status, text="нет")

    async with fetcher_for(tmp_path, robots_or(handler)) as fetcher:
        with pytest.raises(error):
            await fetcher.get("https://uk.example/page")


async def _no_sleep(_: float) -> None:
    return None


async def test_captcha_page_is_blocked_not_parsed(tmp_path):
    def handler(request):
        return httpx.Response(200, text='<html><div class="g-recaptcha"></div></html>', headers={"content-type": "text/html"})

    async with fetcher_for(tmp_path, robots_or(handler)) as fetcher:
        with pytest.raises(AccessBlocked):
            await fetcher.get("https://uk.example/page")


async def test_network_failure_is_retried_then_reported(tmp_path, monkeypatch):
    monkeypatch.setattr("pif.net.client.asyncio.sleep", _no_sleep)
    calls = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(1)
        raise httpx.ConnectTimeout("timeout", request=request)

    async with fetcher_for(tmp_path, handler) as fetcher:
        with pytest.raises(SiteUnavailable):
            await fetcher.get("https://uk.example/page")
    assert len(calls) == 2


def robots_handler(text):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=text, headers={"content-type": "text/plain"})
        return httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"})

    return handler


async def test_robots_closes_pages(tmp_path):
    async with fetcher_for(tmp_path, robots_handler("User-agent: *\nDisallow: /")) as fetcher:
        with pytest.raises(RobotsDisallowed):
            await fetcher.get("https://uk.example/page")
        assert fetcher.stats.robots_denied == 1


async def test_public_document_under_upload_is_fetched_and_marked(tmp_path):
    rules = "User-agent: *\nDisallow: /upload/"
    async with fetcher_for(tmp_path, robots_handler(rules)) as fetcher:
        result = await fetcher.get("https://uk.example/upload/kid.pdf", public_document=True)
        assert fetcher.manifest.get(result.source_id).note == "robots_disallowed_public_document"


async def test_strict_mode_respects_robots_for_documents_too(tmp_path):
    settings = settings_for(tmp_path, strict_robots=True)
    rules = "User-agent: *\nDisallow: /upload/"
    async with fetcher_for(tmp_path, robots_handler(rules), settings=settings) as fetcher:
        with pytest.raises(RobotsDisallowed):
            await fetcher.get("https://uk.example/upload/kid.pdf", public_document=True)


def test_robots_templates_from_real_sites(fixture_text):
    bitrix = RobotsRules.parse(fixture_text("robots_bitrix.txt"), "pif-registry")
    assert not bitrix.allowed("https://www.am-aton.ru/upload/iblock/x.pdf")
    assert bitrix.allowed("https://www.am-aton.ru/legaldocs/docpif")
    closed = RobotsRules.parse(fixture_text("robots_arsagera.txt"), "pif-registry")
    assert not closed.allowed("https://arsagera.ru/products/")


def test_robots_wildcards_and_longest_match():
    rules = RobotsRules.parse("User-agent: *\nDisallow: /*?\nAllow: /funds/*?page=\nDisallow: /*.php$", "x")
    assert not rules.allowed("https://a/funds/1?x=1")
    assert rules.allowed("https://a/funds/1?page=2")
    assert not rules.allowed("https://a/index.php")
    assert rules.allowed("https://a/index.php5")


@pytest.mark.parametrize(
    ("content", "content_type", "url", "expected"),
    [
        (b"%PDF-1.4", "application/octet-stream", "https://x/a", "pdf"),
        (b"<html></html>", "application/pdf", "https://x/a.pdf", "pdf" if False else "pdf"),
        (b"<!doctype html><p>", "", "https://x/a", "html"),
        (b'{"a":1}', "", "https://x/a", "json"),
        (b"\xd0\xcf\x11\xe0abcd", "", "https://x/a.xls", "xls"),
        (b"a;b", "", "https://x/a.csv", "csv"),
    ],
)
def test_format_detection(content, content_type, url, expected):
    assert detect_format(content, content_type, url) == expected


def test_series_store_rerun_changes_nothing(tmp_path):
    day = date(2026, 1, 30)
    store = SeriesStore(tmp_path)
    store.add("r", observation(day, "100", "10"))
    store.save()
    before = (tmp_path / "r.csv").read_bytes()

    again = SeriesStore(tmp_path)
    assert again.add("r", observation(day, "100", "10")) is False
    assert not again.dirty
    again.save()
    assert (tmp_path / "r.csv").read_bytes() == before


def test_daily_point_wins_over_monthly_document(tmp_path):
    day = date(2026, 1, 30)
    store = SeriesStore(tmp_path)
    store.add("r", observation(day, "26690000", "9912.89", kind="monthly_doc"))
    store.add("r", observation(day, "26688123.45", "9912.89", kind="daily"))
    assert store.load("r")[day].nav == D("26688123.45")
    assert store.load("r")[day].kind == "daily"


def test_monthly_document_fills_missing_nav_of_daily_point(tmp_path):
    day = date(2026, 1, 30)
    store = SeriesStore(tmp_path)
    store.add("r", observation(day, None, "10", kind="daily"))
    store.add("r", observation(day, "1000", "10", kind="monthly_doc"))
    point = store.load("r")[day]
    assert point.nav == D("1000") and point.kind == "daily"


def test_source_revision_is_recorded(tmp_path):
    day = date(2026, 1, 30)
    store = SeriesStore(tmp_path)
    store.add("r", observation(day, "100", "10"))
    store.add("r", observation(day, "101", "10"))
    assert store.load("r")[day].nav == D("101")
    assert [(r.field, r.old, r.new) for r in store.revisions] == [("nav", "100", "101")]


async def test_network_failure_on_robots_keeps_previous_rules(tmp_path):
    def closed(request):
        return httpx.Response(200, text="User-agent: *\nDisallow: /", headers={"content-type": "text/plain"})

    async with fetcher_for(tmp_path, closed) as fetcher:
        with pytest.raises(RobotsDisallowed):
            await fetcher.require_open_site("https://uk.example/")

    def broken(request):
        raise httpx.ConnectTimeout("timeout", request=request)

    async with fetcher_for(tmp_path, broken) as fetcher:
        with pytest.raises(RobotsDisallowed):
            await fetcher.require_open_site("https://uk.example/")


async def test_www_and_bare_domain_share_one_rate_limit(tmp_path):
    def handler(request):
        return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})

    settings = settings_for(tmp_path, min_host_interval_s=0.1)
    async with fetcher_for(tmp_path, robots_or(handler), settings=settings) as fetcher:
        started = time.monotonic()
        await asyncio.gather(
            fetcher.get("https://uk.example/a", documented_api=True),
            fetcher.get("https://www.uk.example/b", documented_api=True),
            fetcher.get("https://uk.example/c", documented_api=True),
        )
    assert time.monotonic() - started >= 0.1 * 2 * 0.9
