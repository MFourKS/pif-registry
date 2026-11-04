"""Т-Капитал: документы лежат JSON-списком внутри страницы раздела раскрытия.

Ссылки ведут на cdn.tbank.ru, но те же файлы по тому же пути есть на домене самой УК
cdn.t-capital-funds.ru. КИД на сайте нет, поэтому месячные точки берутся из справок о СЧА.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pif.adapters.base import CollectContext
from pif.adapters.generic import GenericAdapter, Link, link_report_date

SEED = "https://t-capital-funds.ru/documents/mutual_funds/"
_ITEM = re.compile(r'\{"id":"[0-9a-f-]+","name":"')
_STATEMENT = re.compile(r"(?i)^(сча|справка о стоимости чистых активов)\b")
_RETAIL = re.compile(r"(?i)\b(опиф|ипиф|бпиф|открыт\w+|интервальн\w+|биржев\w+)\b")
_CDN = re.compile(r"^https://cdn\.tbank\.ru/")
MIRROR = "https://cdn.t-capital-funds.ru/"


@dataclass(frozen=True, slots=True)
class Document:
    name: str
    url: str


def documents_in(page: str) -> list[Document]:
    found: list[Document] = []
    decoder = json.JSONDecoder()
    for match in _ITEM.finditer(page):
        try:
            item = decoder.raw_decode(page, match.start())[0]
        except json.JSONDecodeError:
            continue
        if item.get("link") and item.get("name"):
            found.append(Document(item["name"], _CDN.sub(MIRROR, item["link"])))
    return found


class TCapitalAdapter(GenericAdapter):
    async def collect(self, ctx: CollectContext) -> None:
        page = await ctx.fetch(SEED)
        ctx.counters["pages"] += 1
        wanted = {}
        for document in documents_in(page.text):
            if not _STATEMENT.search(document.name) or not _RETAIL.search(document.name):
                continue
            day = link_report_date(document.name, document.url)
            if day is None or day < ctx.cutoff:
                continue
            wanted[document.url] = Link(document.url, document.name, SEED)
        for link in wanted.values():
            await self.collect_statement(ctx, link)
