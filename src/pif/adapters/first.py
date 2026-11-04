"""УК Первая: дневные ряды на страницах фондов, официальная доходность в таблице раскрытия.

Таблица на /disclosure/fund содержит прирост расчётной стоимости пая за 1, 3, 6 и 12 месяцев
по всем фондам сразу. Для самопроверки это такой же официальный источник, как КИД.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import lxml.html

from pif.adapters.base import CollectContext
from pif.adapters.generic import GenericAdapter
from pif.errors import UnknownFormatError
from pif.models import Fund, FundType, OfficialReturn
from pif.parsers.numbers import parse_date, parse_percent

DISCLOSURE = "https://www.first-am.ru/disclosure/fund"
_SECTIONS = {
    "Открытые": FundType.OPEN,
    "Биржевые": FundType.EXCHANGE,
    "Интервальные": FundType.INTERVAL,
}
_PERIOD_COLUMNS = {2: 1, 3: 3, 4: 6, 5: 12}
_TICKER = re.compile(r"\s[-–—]\s([A-Z0-9]{3,6})\s*$")


@dataclass(frozen=True, slots=True)
class ReturnRow:
    fund_type: FundType
    name: str
    ticker: str | None
    report_date: date
    returns: dict[int, Decimal]


def parse_returns_table(page: str, source: str) -> list[ReturnRow]:
    document = lxml.html.fromstring(page)
    block = document if document.get("id") == "pif-table" else document.get_element_by_id("pif-table", None)
    if block is None:
        raise UnknownFormatError(source, "на странице нет таблицы прироста стоимости пая")
    rows: list[ReturnRow] = []
    section: FundType | None = None
    for element in block.iter("h4", "tr"):
        if element.tag == "h4":
            title = " ".join(element.text_content().split())
            section = next((kind for word, kind in _SECTIONS.items() if title.startswith(word)), None)
            continue
        cells = [" ".join(cell.text_content().split()) for cell in element.iterchildren("td")]
        day = parse_date(cells[1]) if len(cells) >= 6 else None
        if section is None or day is None:
            continue
        returns = {m: v for i, m in _PERIOD_COLUMNS.items() if (v := parse_percent(cells[i])) is not None}
        ticker = _TICKER.search(cells[0])
        rows.append(ReturnRow(section, cells[0], ticker[1] if ticker else None, day, returns))
    if not rows:
        raise UnknownFormatError(source, "таблица прироста стоимости пая пуста")
    return rows


class FirstAdapter(GenericAdapter):
    async def collect(self, ctx: CollectContext) -> None:
        await super().collect(ctx)
        result = await ctx.try_fetch(DISCLOSURE)
        if result is None:
            return
        by_ticker = {f.ticker: f for f in ctx.funds if f.ticker}
        for row in parse_returns_table(result.text, result.url):
            fund: Fund | None = by_ticker.get(row.ticker or "") or ctx.matcher.match(row.name, fund_type=row.fund_type)
            if fund is None:
                ctx.note_unmatched(result.url, row.name)
                continue
            ctx.fetcher.manifest_link(result, ctx.uk.inn, fund.rule_number)
            for months, value in row.returns.items():
                ctx.official.add(OfficialReturn(fund.rule_number, row.report_date, months, value, result.source_id))
