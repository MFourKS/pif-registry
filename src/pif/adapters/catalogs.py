"""УК, которые отдают каталог документов и ряды в JSON: Герои и Озон Управление Активами."""

from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import urljoin

from pif.adapters.base import CollectContext
from pif.adapters.generic import GenericAdapter, Link, is_kid_link, is_statement_link, link_report_date
from pif.errors import ParseError
from pif.parsers.numbers import parse_date
from pif.parsers.series import json_records


def _file_date(url: str) -> date | None:
    """key_info_30_09_2026.pdf: дата документа в имени файла."""
    found = re.search(r"(\d{2})_(\d{2})_(20\d{2})", url)
    return parse_date(f"{found[1]}.{found[2]}.{found[3]}") if found else None


class GeroiAdapter(GenericAdapter):
    """Герои: /disclosure.json перечисляет файлы, /funds?id=N отдаёт дневной ряд."""

    SITE = "https://ih-capital.ru"

    async def collect(self, ctx: CollectContext) -> None:
        catalog = await ctx.fetch(f"{self.SITE}/disclosure.json")
        ctx.counters["pages"] += 1
        for section in json.loads(catalog.content):
            name = section.get("section", "")
            fund = ctx.matcher.match(name)
            if fund is None:
                continue
            if section.get("dataUrl"):
                result = await ctx.try_fetch(urljoin(self.SITE, section["dataUrl"]), fund=fund)
                if result is not None:
                    try:
                        ctx.add_series(fund, json_records(json.loads(result.content), result.url), result)
                    except ParseError as exc:
                        ctx.note_parse_error(result.url, exc, fund)
            for item in section.get("files", []):
                link = Link(urljoin(self.SITE, item["url"]), item.get("name", ""), catalog.url)
                day = link_report_date(link.text, link.url) or _file_date(link.url)
                if not is_kid_link(link) or (day is not None and day < ctx.cutoff):
                    continue
                await self.collect_kid(ctx, link, fund=fund)


class OzonAdapter(GenericAdapter):
    """Озон: список фондов и документы по тикеру через API сайта."""

    SITE = "https://ozon-am.ru"

    async def collect(self, ctx: CollectContext) -> None:
        listing = await ctx.fetch(f"{self.SITE}/api/mutual-funds")
        ctx.counters["pages"] += 1
        for item in json.loads(listing.content)["data"]:
            fund = ctx.matcher.match(item["mutualFundName"])
            if fund is None:
                ctx.note_unmatched(listing.url, item["mutualFundName"])
                continue
            documents = await ctx.try_fetch(
                f"{self.SITE}/api/documents", params={"ticker": item["ticker"]}, fund=fund
            )
            if documents is None:
                continue
            for group in json.loads(documents.content)["data"]:
                for document in group.get("items", []):
                    link = Link(document["link"], document["documentName"], documents.url)
                    if is_kid_link(link):
                        await self.collect_kid(ctx, link, fund=fund)
                    elif is_statement_link(link):
                        await self.collect_statement(ctx, link)
