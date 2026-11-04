"""ВИМ Инвестиции: выгрузка котировок download.php с диапазоном дат.

Страница фонда показывает только последние точки, но содержит форму экспорта с параметрами
фонда. Выгрузка приходит HTML-таблицей, поэтому её разбирает общий html_table.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urljoin

from pif.adapters.base import CollectContext
from pif.adapters.generic import GenericAdapter
from pif.errors import ParseError
from pif.models import Fund
from pif.net.client import FetchResult
from pif.parsers.series import html_table

_EXPORT_FORM = re.compile(r'(?is)<form[^>]+action="([^"]*download\.php)"[^>]*id="export"[^>]*>(.*?)</form>')
_FIELD = re.compile(r'(?i)<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"')


def export_request(page: str, base: str, start: date, end: date) -> tuple[str, dict[str, str]] | None:
    match = _EXPORT_FORM.search(page)
    if match is None:
        return None
    params = dict(_FIELD.findall(match[2]))
    params["from"] = start.strftime("%d.%m.%Y")
    params["to"] = end.strftime("%d.%m.%Y")
    return urljoin(base, match[1]), params


class WimAdapter(GenericAdapter):
    def harvest_series(self, ctx: CollectContext, text: str, result: FetchResult) -> None:
        # Короткая таблица на странице только мешает: полный ряд дадут выгрузки ниже.
        pending = getattr(ctx, "wim_pages", None)
        if pending is None:
            pending = ctx.wim_pages = {}  # type: ignore[attr-defined]
        if _EXPORT_FORM.search(text):
            pending[result.url] = text

    async def collect(self, ctx: CollectContext) -> None:
        await super().collect(ctx)
        last_day = date.today()
        for url, page in getattr(ctx, "wim_pages", {}).items():
            fund = self._fund_of(ctx, page, url)
            if fund is None:
                ctx.note_unmatched(url, "страница ВИМ с формой выгрузки")
                continue
            known = ctx.series.last_date(fund.rule_number)
            start = known - timedelta(days=7) if known else ctx.cutoff
            request = export_request(page, url, start, last_day)
            if request is None:
                continue
            target, params = request
            result = await ctx.try_fetch(target, params=params, fund=fund)
            if result is None:
                continue
            try:
                rows = html_table(result.text, result.url)
            except ParseError as exc:
                ctx.note_parse_error(result.url, exc, fund)
                continue
            ctx.add_series(fund, rows, result)

    @staticmethod
    def _fund_of(ctx: CollectContext, page: str, url: str) -> Fund | None:
        from pif.adapters.generic import page_identity

        return page_identity(ctx, page, url)
