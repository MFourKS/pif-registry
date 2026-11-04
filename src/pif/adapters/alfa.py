"""Альфа-Капитал: SSR-состояние страницы раскрытия и шлюз истории fundDisclosureRegData."""

from __future__ import annotations

import json
from typing import Any

from pif.adapters.base import CollectContext
from pif.adapters.generic import GenericAdapter, Link, link_report_date
from pif.errors import UnknownFormatError
from pif.models import Fund
from pif.observability import get_logger, tracer
from pif.parsers.series import SeriesRow, balanced_json, json_records

log = get_logger("adapter.alfa")

SITE = "https://www.alfacapital.ru"
STATE_MARK = "window.__SERVER_STATE__ = "
HISTORY_GATE = f"{SITE}/gate/v1/fundDisclosureRegData/v1"
_CATEGORIES = {"pifs": ("fundsOpened", "fundsInterval", "fundsExchange")}


def server_state(page: str, source: str) -> dict[str, Any]:
    start = page.find(STATE_MARK)
    if start < 0:
        raise UnknownFormatError(source, "на странице нет __SERVER_STATE__")
    return balanced_json(page, start + len(STATE_MARK))


def fund_slugs(state: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Список (slug, название, тип) из дерева фондов раздела раскрытия."""
    result: list[tuple[str, str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "slug" in node and "fullName" in node:
                result.append((node["slug"], node["fullName"], node.get("fundType", "")))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(state.get("disclosureFunds", {}))
    return result


class AlfaAdapter(GenericAdapter):
    async def collect(self, ctx: CollectContext) -> None:
        listing = await ctx.fetch(f"{SITE}/disclosure/pifs")
        slugs = fund_slugs(server_state(listing.text, listing.url))
        if not slugs:
            raise UnknownFormatError(listing.url, "пустой список фондов в разделе раскрытия")
        known = {slug: rule for rule, slug in ctx.config.options.get("slugs", {}).items()}
        for slug, name, _ in slugs:
            fund = ctx.matcher.by_rule.get(known.get(slug, "")) or ctx.matcher.match(name)
            if fund is None:
                ctx.note_unmatched(listing.url, name)
                continue
            with tracer().start_as_current_span("adapter.fund") as span:
                span.set_attribute("pif.fund", fund.rule_number)
                await self._history(ctx, fund, slug)
                await self._kids(ctx, fund, slug)

    async def _history(self, ctx: CollectContext, fund: Fund, slug: str) -> None:
        known_until = ctx.series.last_date(fund.rule_number)
        page_number = 0
        while True:
            result = await ctx.try_fetch(HISTORY_GATE, params={"slug": slug, "page": page_number}, fund=fund)
            if result is None:
                return
            payload = json.loads(result.content)
            records = payload.get("data") or []
            if not records:
                return
            rows: list[SeriesRow] = json_records(records, result.url)
            ctx.add_series(fund, rows, result)
            oldest = min(row.day for row in rows)
            # История отдаётся от новых точек к старым: докачка останавливается на известной дате.
            if oldest < ctx.cutoff or (known_until and oldest <= known_until):
                return
            page_number += 1

    async def _kids(self, ctx: CollectContext, fund: Fund, slug: str) -> None:
        url = f"{SITE}/disclosure/pifs/{slug}/information-docs"
        result = await ctx.try_fetch(url, fund=fund)
        if result is None:
            return
        try:
            documents = server_state(result.text, url)["disclosure"]["fund"]["documents"]["documents"]
        except UnknownFormatError:
            # У новых фондов вкладки документов ещё нет: сайт отдаёт обычную страницу без состояния.
            ctx.counters["no_documents_page"] += 1
            return
        for document in documents:
            link = Link(f"{SITE}{document['path']}", document["name"], url)
            await self.collect_kid_for(ctx, fund, link)

    async def collect_kid_for(self, ctx: CollectContext, fund: Fund, link: Link) -> None:
        day = link_report_date(link.text, link.url)
        if day is None or day >= ctx.cutoff:
            await self.collect_kid(ctx, link, fund=fund)
