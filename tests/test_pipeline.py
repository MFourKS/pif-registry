"""Изоляция сбоев, ворота публикации и ограничения статического сайта."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from conftest import FIXTURES, make_fund, observation
from pif.adapters.base import Adapter, CollectContext, FundOutcome, Issue, UkConfig
from pif.adapters.generic import Link, is_kid_link, link_report_date
from pif.adapters.wim import export_request
from pif.analytics import build_analysis, classify_status, quality_report
from pif.collect import CollectResult, _collect_uk, load_sources
from pif.config import Settings
from pif.errors import SiteUnavailable
from pif.models import DataStatus, Fund, FundType, ManagementCompany
from pif.net.client import Fetcher
from pif.normalize.fx import FxRates
from pif.normalize.series import normalize
from pif.parsers.pool import DocumentParser
from pif.registry.build import Registry
from pif.site.build import build_site
from pif.storage.manifest import Manifest
from pif.storage.rawstore import RawStore
from pif.storage.series_store import OfficialStore, SeriesStore

AS_OF = date(2026, 11, 4)


def company(inn: str, name: str = "УК") -> ManagementCompany:
    return ManagementCompany(inn, "1", name, name, "uk.example", "7")


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        raw_dir=tmp_path / "raw",
        state_dir=tmp_path / "state",
        out_dir=tmp_path / "out",
        dist_dir=tmp_path / "dist",
        config_dir=tmp_path / "config",
        manifest_path=tmp_path / "manifest" / "sources.csv",
        min_host_interval_s=0,
        base_path="/nested/pif/",
    )


def registry_of(*funds: Fund) -> Registry:
    companies = {f.uk_inn: company(f.uk_inn, f.uk_name) for f in funds}
    return Registry(AS_OF, "https://www.cbr.ru/x.xlsx", "src", list(funds), companies)


class Broken(Adapter):
    async def collect(self, ctx: CollectContext) -> None:
        raise SiteUnavailable("https://uk.example", "таймаут")


class Crashing(Adapter):
    async def collect(self, ctx: CollectContext) -> None:
        raise ValueError("неожиданный формат")


@pytest.mark.parametrize(("adapter", "severity"), [(Broken, "warning"), (Crashing, "error")])
async def test_broken_site_does_not_stop_collection(tmp_path, monkeypatch, adapter, severity):
    import asyncio

    monkeypatch.setitem(__import__("pif.adapters").adapters.ADAPTERS, "broken", adapter)
    settings = settings_for(tmp_path)
    fund = make_fund()
    registry = registry_of(fund)
    configs = {fund.uk_inn: UkConfig(adapter="broken")}
    result = CollectResult()
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    async with Fetcher(settings, RawStore(settings.raw_dir), Manifest(settings.manifest_path), transport=transport) as fetcher:
        await _collect_uk(
            registry.companies[fund.uk_inn],
            settings,
            registry,
            configs,
            fetcher,
            SeriesStore(settings.series_dir),
            OfficialStore(settings.official_dir / "r.csv"),
            result,
            asyncio.Semaphore(1),
            DocumentParser(settings.state_dir / "parsed.json", workers=1),
        )
    assert result.issues[0].severity == severity
    assert fund.uk_inn in result.uk_failures


def analysis_for(tmp_path: Path, funds: list[Fund], points: dict[str, list[tuple[date, str, str]]], collected=None):
    settings = settings_for(tmp_path)
    store = SeriesStore(settings.series_dir)
    for rule, rows in points.items():
        for day, nav, price in rows:
            store.add(rule, observation(day, nav, price))
    official = OfficialStore(settings.official_dir / "r.csv")
    analysis = build_analysis(settings, registry_of(*funds), store, official, FxRates(), collected)
    return settings, analysis


def month_ends(months: int = 14) -> list[date]:
    days = []
    year, month = 2026, 10
    for _ in range(months):
        first_next = date(year + (month == 12), month % 12 + 1, 1)
        days.append(date.fromordinal(first_next.toordinal() - 1))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return sorted(days)


def test_quality_gate_blocks_on_collection_errors_only(tmp_path):
    fund = make_fund()
    rows = [(d, "1000", "100") for d in month_ends()]
    settings, analysis = analysis_for(tmp_path, [fund], {fund.rule_number: rows})
    assert quality_report(analysis, [], settings, Manifest(settings.manifest_path))["publish_allowed"] is True

    warning = Issue("warning", "fetch", fund.uk_inn, "https://x", "сайт недоступен")
    assert quality_report(analysis, [warning], settings, Manifest(settings.manifest_path))["publish_allowed"] is True

    error = Issue("error", "parse", fund.uk_inn, "https://x", "новый формат")
    report = quality_report(analysis, [error], settings, Manifest(settings.manifest_path))
    assert report["publish_allowed"] is False
    assert report["errors"][0]["message"] == "новый формат"


def test_yield_mismatch_does_not_block_publication(tmp_path):
    from pif.models import OfficialReturn

    fund = make_fund()
    settings = settings_for(tmp_path)
    store = SeriesStore(settings.series_dir)
    for day in month_ends():
        store.add(fund.rule_number, observation(day, "1000", "100"))
    official = OfficialStore(settings.official_dir / "r.csv")
    official.add(OfficialReturn(fund.rule_number, date(2026, 10, 31), 1, D("5"), "src"))
    analysis = build_analysis(settings, registry_of(fund), store, official, FxRates(), None)
    report = quality_report(analysis, [], settings, Manifest(settings.manifest_path))
    assert len(report["yield_check"]["mismatches"]) == 1
    assert report["publish_allowed"] is True
    assert analysis.views[fund.rule_number].mismatches


@pytest.mark.parametrize(
    ("fund_kwargs", "days", "outcome", "uk_failure", "expected"),
    [
        ({}, month_ends(), None, None, DataStatus.OK),
        ({"formed_on": date(2026, 6, 1)}, [date(2026, 6, 30), date(2026, 7, 31), date(2026, 8, 31)], None, None, DataStatus.FORMED_LATER),
        ({"cbr_status": "Формируется", "formed_on": None}, [], None, None, DataStatus.NOT_FORMED),
        ({}, [], None, (DataStatus.SITE_UNAVAILABLE, "таймаут"), DataStatus.SITE_UNAVAILABLE),
        ({}, [], None, (DataStatus.BLOCKED, "403"), DataStatus.BLOCKED),
        ({}, [], FundOutcome(failure=DataStatus.PARSE_ERROR, failure_reason="формат"), None, DataStatus.PARSE_ERROR),
        ({}, [], None, None, DataStatus.NOT_FOUND),
        ({}, [date(2026, 10, 31)], None, None, DataStatus.NO_HISTORY),
        ({}, [date(2026, 6, 30), date(2026, 7, 31), date(2026, 8, 31)], None, None, DataStatus.PARTIAL),
    ],
)
def test_status_explains_why_there_is_no_series(fund_kwargs, days, outcome, uk_failure, expected):
    fund = make_fund(**fund_kwargs)
    series = normalize(fund.rule_number, {d: observation(d, "1000", "100", kind="monthly_doc") for d in days}, FxRates())
    status, reason = classify_status(fund, series, outcome, uk_failure, AS_OF)
    assert status == expected
    assert expected == DataStatus.OK or reason


def built_site(tmp_path: Path) -> Path:
    funds = [
        make_fund("0001-00000001", uk_inn="7700000001", uk_name="УК Один"),
        make_fund("8167-СД", uk_inn="7700000002", uk_name="УК Два", fund_type=FundType.INTERVAL, for_qualified=True),
    ]
    rows = [(d, str(1000 + i * 10), str(100 + i)) for i, d in enumerate(month_ends())]
    settings, analysis = analysis_for(tmp_path, funds, {funds[0].rule_number: rows})
    settings.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(settings.manifest_path)
    manifest.save()
    report = quality_report(analysis, [], settings, Manifest(settings.manifest_path))
    return build_site(settings, analysis, manifest, report)


def test_site_has_clean_urls_and_ready_html(tmp_path):
    dist = built_site(tmp_path)
    assert (dist / "fund" / "0001-00000001" / "index.html").exists()
    assert (dist / "fund" / "8167-СД" / "index.html").exists()
    assert (dist / "uk" / "7700000001" / "index.html").exists()
    assert (dist / "market" / "index.html").exists()
    page = (dist / "fund" / "0001-00000001" / "index.html").read_text(encoding="utf-8")
    assert "<svg" in page, "графики приходят готовым HTML"
    assert "инвестиционн" in page and "рекомендаци" in page
    rating = (dist / "index.html").read_text(encoding="utf-8")
    assert "рекомендаци" in rating
    assert "Банк России" in rating and "04.11.2026" in rating


def test_site_loads_nothing_from_outside_and_works_from_subfolder(tmp_path):
    dist = built_site(tmp_path)
    for path in dist.rglob("*.html"):
        html = path.read_text(encoding="utf-8")
        for tag, url in re.findall(r'<(script|link|img|iframe)[^>]+(?:src|href)="([^"]+)"', html):
            assert not url.startswith(("http:", "https:", "//", "/")), f"{path}: {tag} {url}"
    css = (dist / "assets" / "app.css").read_text(encoding="utf-8")
    assert "http" not in css
    assert 'url(fonts/manrope-cyrillic.woff2)' in css
    assert (dist / "assets" / "fonts" / "manrope-cyrillic.woff2").exists()


def test_site_build_is_deterministic(tmp_path):
    first = built_site(tmp_path / "a")
    second = built_site(tmp_path / "b")
    digests = []
    for dist in (first, second):
        digests.append(
            {
                str(p.relative_to(dist)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(dist.rglob("*"))
                if p.is_file()
            }
        )
    assert digests[0] == digests[1]


def test_fund_without_data_still_has_a_page_with_reason(tmp_path):
    dist = built_site(tmp_path)
    page = (dist / "fund" / "8167-СД" / "index.html").read_text(encoding="utf-8")
    assert "Данные фонда не найдены" in page


@pytest.mark.parametrize(
    ("text", "url", "expected"),
    [
        ("Ключевой информационный документ на 30.09.2026", "https://x/a.pdf", date(2026, 9, 30)),
        ("КИД за сентябрь 2026", "https://x/a.pdf", date(2026, 9, 28)),
        ("Скачать", "https://x/upload/kid_3146_2026-9.pdf", date(2026, 9, 28)),
        ("Документ", "https://x/a.pdf", None),
    ],
)
def test_link_report_date(text, url, expected):
    assert link_report_date(text, url) == expected


@pytest.mark.parametrize(
    ("text", "url", "expected"),
    [
        ("Ключевой информационный документ на 30.09.2026", "https://x/upload/a.pdf", True),
        ("КИД", "https://x/doc/1/view", True),
        ("Сообщение о корректировке ключевых информационных документов", "https://x/a.pdf", False),
        ("Ключевая информация об обработке персональных данных", "https://x/a.pdf", False),
        ("Правила фонда", "https://x/a.pdf", False),
    ],
)
def test_kid_link_recognition(text, url, expected):
    assert is_kid_link(Link(url, text, "https://x/")) is expected


def test_wim_export_request_comes_from_page_form():
    page = (
        '<form action="/local/download.php" id="export" method="get">'
        '<input type="hidden" name="FOND_ID" value="30148"/><input type="hidden" name="from" value=""/></form>'
    )
    url, params = export_request(page, "https://www.wealthim.ru/x/", date(2025, 7, 1), date(2026, 11, 4))
    assert url == "https://www.wealthim.ru/local/download.php"
    assert params == {"FOND_ID": "30148", "from": "01.07.2025", "to": "04.11.2026"}


def test_sources_config_is_valid():
    from pif.adapters import ADAPTERS

    configs = load_sources(Path(__file__).parents[1] / "config" / "sources.yaml")
    assert configs
    for inn, config in configs.items():
        assert re.fullmatch(r"\d{10}", inn)
        assert config.adapter in ADAPTERS
        if config.follow:
            re.compile(config.follow)


def test_fixtures_were_not_damaged():
    """Контрольные суммы: повреждённая фикстура роняет сборку, даже если тесты её не читают."""
    expected = {}
    for line in (FIXTURES / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split(maxsplit=1)
        expected[name.lstrip("*")] = digest
    actual = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in FIXTURES.iterdir()
        if p.name not in {"SHA256SUMS", "PROVENANCE.md"}
    }
    assert actual == expected



def test_full_pipeline_from_stored_data(tmp_path, monkeypatch):
    """Команда build проходит весь путь от накопленных рядов до сайта и отчётов."""
    import json

    from pif import cli

    funds = [make_fund("0001-00000001", uk_inn="7700000001", uk_name="УК Один")]
    settings = settings_for(tmp_path)
    registry_of(*funds).save(settings.data_dir / "registry.json")
    store = SeriesStore(settings.series_dir)
    for index, day in enumerate(month_ends()):
        store.add(funds[0].rule_number, observation(day, str(1000 + index * 10), str(100 + index)))
    store.save()
    settings.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    code = cli._pipeline(settings, force=False, only=None, skip_collect=True, build=True)

    assert code == 0
    report = json.loads((settings.data_dir / "quality_report.json").read_text(encoding="utf-8"))
    assert report["funds_with_series"] == 1
    assert (settings.dist_dir / "nested" / "pif" / "index.html").exists()
    assert (settings.out_dir / "last_run.json").exists()
    assert list((settings.out_dir / "traces").glob("*.jsonl"))
