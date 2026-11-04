"""Универсальный адаптер: обход сайта УК, поиск встроенных рядов, файлов и КИД.

Подходит большинству УК. Настройка на конкретную компанию — это стартовые адреса и шаблон ссылок
в config/sources.yaml. Новый код нужен только там, где данные отдаёт собственный API.
"""

from __future__ import annotations

import ipaddress
import re
from collections import deque
from dataclasses import dataclass
from datetime import date
from urllib.parse import urldefrag, urljoin, urlsplit

import lxml.html

from pif.adapters.base import Adapter, CollectContext
from pif.errors import ParseError
from pif.models import Fund
from pif.net.client import FetchResult
from pif.observability import get_logger, tracer
from pif.parsers import series as series_parsers
from pif.parsers.embedded import MIN_POINTS, extract_series
from pif.parsers.kid import parse_kid
from pif.parsers.numbers import parse_date

log = get_logger("adapter.generic")

_DEFAULT_FOLLOW = re.compile(
    r"(?i)(pif|fund|fond|opif|ipif|bpif|etf|product|disclos|raskr|information|info|kid|invest|paev|compliance|"
    r"фонд|пиф|раскрыт)"
)
_SKIP = re.compile(
    r"(?i)(\.(jpe?g|png|gif|svg|webp|ico|css|js|woff2?|ttf|zip|rar|sig|p7s|mp4)(\?|$)|/news|/press|/blog|"
    r"/vacan|/career|/analytic|/login|/auth|/lk/|mailto:|tel:|javascript:|/search|/contacts?/?$|zpif|/en/|"
    r"/personal|/cabinet|/bitrix/|/ajax)"
)
_KID_TEXT = re.compile(r"(?i)ключев\w*\s+информ\w*\s+документ|(?<![а-яё])кид(?![а-яё])")
_KID_HREF = re.compile(r"(?i)(^|[/_\-.])(kid|klyuchevoy|kluchevoy|klyuchevoi|key[_-]?info)")
_NOT_KID = re.compile(r"(?i)договор|сообщени|корректиров|неточн|исправлен\w+ сведен|прекращ|изменени[йя] в|архив")
_DOC_SUFFIX = re.compile(r"(?i)\.(pdf|docx?)(\?|$)")
_DATA_FILE = re.compile(r"(?i)\.(csv|xlsx?)(\?|$)")
_DATA_FILE_HINT = re.compile(r"(?i)(cost|price|nav|scha|стоимост|сча|динамик|истори|пай|пая)")
_MONTH_YEAR = re.compile(r"(?i)(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*\s+(20\d\d)")
_MONTHS = ("январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр")


@dataclass(frozen=True, slots=True)
class Link:
    url: str
    text: str
    page: str


def _links(page: str, base: str) -> list[Link]:
    try:
        document = lxml.html.fromstring(page)
    except (lxml.etree.ParserError, ValueError):
        return []
    # <base href> меняет разрешение относительных ссылок, так свёрстан, например, сайт УК ДОХОДЪ.
    base_tag = document.find(".//base")
    resolve_from = urljoin(base, base_tag.get("href") or "") if base_tag is not None else base
    links: list[Link] = []
    for anchor in document.iter("a"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        text = " ".join(f"{anchor.text_content()} {anchor.get('title') or ''}".split())
        # На сайтах с таблицами документов название лежит в соседней ячейке, а ссылка — «Скачать».
        if len(text) < 12:
            parent = anchor.getparent()
            if parent is not None:
                row = parent.getparent() if parent.tag == "td" else parent
                if row is not None:
                    text = " ".join(row.text_content().split())[:240]
        links.append(Link(urldefrag(urljoin(resolve_from, href))[0], text, base))
    return links


def link_report_date(text: str, url: str) -> date | None:
    """Дата документа по подписи: «на 30.09.2026», «за сентябрь 2026», kid_3146_2026-9.pdf."""
    day = parse_date(text)
    if day:
        return day
    month = _MONTH_YEAR.search(text)
    if month:
        stem = month[1].lower()
        number = next(i for i, name in enumerate(_MONTHS, 1) if stem.startswith(name))
        return date(int(month[2]), number, 28)
    found = re.search(r"(20\d\d)[-_.](\d{1,2})(?:[-_.](\d{1,2}))?(?=\D|$)", urlsplit(url).path)
    if found and 1 <= int(found[2]) <= 12:
        return date(int(found[1]), int(found[2]), 28)
    return None


def is_kid_link(link: Link) -> bool:
    label = f"{link.text} {link.url}"
    if _NOT_KID.search(link.text):
        return False
    return bool(_KID_TEXT.search(link.text) or _KID_HREF.search(urlsplit(link.url).path)) and bool(
        _DOC_SUFFIX.search(link.url) or re.search(r"(?i)upload|download|/file|/doc|attachments|cdn|storage", label)
    )


def page_identity(ctx: CollectContext, page: str, url: str) -> Fund | None:
    """Определяет фонд страницы: явная привязка в настройках, затем заголовки, затем номер правил."""
    path = urlsplit(url).path
    for fragment, rule in ctx.config.fund_map.items():
        if fragment in path:
            return ctx.matcher.by_rule.get(rule)
    head = page[:400_000]
    candidates = re.findall(r"(?is)<h1[^>]*>(.*?)</h1>", head)
    candidates += re.findall(r"(?is)<title[^>]*>(.*?)</title>", head)
    candidates += re.findall(r'(?is)<meta[^>]+property="og:title"[^>]+content="([^"]+)"', head)
    for raw in candidates:
        text = " ".join(re.sub(r"<[^>]+>", " ", raw).split())
        fund = ctx.matcher.match(text)
        if fund:
            return fund
    # Страница одного фонда обычно содержит его номер правил. Подвал со всеми номерами сюда не пройдёт.
    return ctx.matcher.by_text_rule(page)


class GenericAdapter(Adapter):
    async def collect(self, ctx: CollectContext) -> None:
        seeds = ctx.config.seeds or ([_absolute(ctx.uk.site)] if ctx.uk.site else [])
        if not seeds:
            return
        kid_links = await self.crawl(ctx, seeds)
        await self.collect_kids(ctx, kid_links)

    async def crawl(self, ctx: CollectContext, seeds: list[str]) -> dict[str, Link]:
        follow = re.compile(ctx.config.follow) if ctx.config.follow else _DEFAULT_FOLLOW
        skip = re.compile(ctx.config.skip) if ctx.config.skip else None
        hosts = {urlsplit(s).netloc.removeprefix("www.") for s in seeds}
        queue = deque(seeds)
        seen: set[str] = set(seeds)
        kid_links: dict[str, Link] = {}
        data_files: dict[str, Link] = {}
        pages = 0

        with tracer().start_as_current_span("adapter.crawl") as span:
            while queue and pages < ctx.config.max_pages:
                url = queue.popleft()
                result = await ctx.try_fetch(url)
                if result is None or result.fmt != "html":
                    continue
                pages += 1
                text = result.text
                self.harvest_series(ctx, text, result)
                for link in _links(text, result.final_url):
                    if is_kid_link(link):
                        kid_links.setdefault(link.url, link)
                        continue
                    if _DATA_FILE.search(link.url) and _DATA_FILE_HINT.search(f"{link.text} {link.url}"):
                        data_files.setdefault(link.url, link)
                        continue
                    parts = urlsplit(link.url)
                    if (
                        link.url in seen
                        or _is_private_host(parts.hostname)
                        or parts.netloc.removeprefix("www.") not in hosts
                        or _SKIP.search(link.url)
                        or _DOC_SUFFIX.search(link.url)
                        or (skip and skip.search(link.url))
                        or not follow.search(parts.path + "?" + parts.query)
                    ):
                        continue
                    seen.add(link.url)
                    queue.append(link.url)
            span.set_attribute("pif.pages", pages)
            span.set_attribute("pif.kid_links", len(kid_links))
            span.set_attribute("pif.data_files", len(data_files))

        await self.collect_data_files(ctx, data_files)
        ctx.counters["pages"] += pages
        return kid_links

    def harvest_series(self, ctx: CollectContext, text: str, result: FetchResult) -> None:
        try:
            found = extract_series(text, result.url)
        except ParseError as exc:
            ctx.note_parse_error(result.url, exc)
            return
        if not found:
            return
        recognizer, rows = found
        fund = page_identity(ctx, text, result.url)
        if fund is None:
            ctx.note_unmatched(result.url, f"ряд из {len(rows)} точек ({recognizer})")
            return
        if ctx.outcomes[fund.rule_number].daily_points >= len(rows):
            return
        ctx.add_series(fund, rows, result)
        log.debug("series_harvested", fund=fund.rule_number, rows=len(rows), recognizer=recognizer)

    async def collect_data_files(self, ctx: CollectContext, files: dict[str, Link]) -> None:
        for link in files.values():
            page = await ctx.try_fetch(link.page)
            fund = page_identity(ctx, page.text, link.page) if page else None
            fund = fund or ctx.matcher.match(link.text)
            if fund is None or ctx.outcomes[fund.rule_number].daily_points > 20:
                continue
            result = await ctx.try_fetch(link.url, fund=fund, public_document=True)
            if result is None:
                continue
            try:
                if result.fmt in {"xlsx", "xls"}:
                    rows = series_parsers.spreadsheet(result.content, result.fmt, result.url)
                else:
                    rows = series_parsers.delimited_text(result.text, result.url)
            except ParseError as exc:
                ctx.issues.append(_warn(ctx, link.url, f"Файл не содержит ряда: {exc}"))
                continue
            if len(rows) >= MIN_POINTS:
                ctx.add_series(fund, rows, result)

    async def collect_kids(self, ctx: CollectContext, links: dict[str, Link]) -> None:
        wanted: list[Link] = []
        undated = 0
        for link in links.values():
            day = link_report_date(link.text, link.url)
            if day is None:
                undated += 1
                if undated > ctx.config.options.get("max_undated_kids", 40) * max(1, len(ctx.funds)):
                    continue
            elif day < ctx.cutoff:
                continue
            wanted.append(link)
        with tracer().start_as_current_span("adapter.kids") as span:
            span.set_attribute("pif.kid_wanted", len(wanted))
            for link in sorted(wanted, key=lambda item: item.url):
                await self.collect_kid(ctx, link)

    async def collect_kid(self, ctx: CollectContext, link: Link) -> None:
        # КИД не меняется после публикации, поэтому проверять его заново незачем.
        result = await ctx.try_fetch(link.url, ttl=180 * 86400, public_document=True)
        if result is None:
            return
        if result.fmt not in {"pdf", "docx"}:
            ctx.counters["kid_not_a_document"] += 1
            return
        try:
            kid = parse_kid(result.content, result.fmt, link.url)
        except ParseError as exc:
            ctx.note_parse_error(link.url, exc)
            return
        fund = None
        if kid.fund_name:
            fund = ctx.matcher.match(kid.fund_name)
        fund = fund or ctx.matcher.match(link.text)
        if fund is None:
            ctx.note_unmatched(link.url, f"КИД: {kid.fund_name or link.text}")
            return
        ctx.add_kid(fund, kid, result)


def _is_private_host(host: str | None) -> bool:
    """На одном из сайтов нашлись ссылки на внутренний адрес 192.168.x.x: туда ходить незачем."""
    try:
        return host is not None and ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _warn(ctx: CollectContext, url: str, message: str):
    from pif.adapters.base import Issue

    return Issue("warning", "parse", ctx.uk.inn, url, message[:240])


def _absolute(site: str | None) -> str:
    if not site:
        return ""
    return site if site.startswith("http") else f"https://{site}"
