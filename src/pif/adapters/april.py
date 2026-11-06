"""Апрель Капитал: сайт на Nuxt, данные отдаёт собственное API.

`fund_details_list` возвращает дневной ряд пая и СЧА в копейках, `files_list` перечисляет КИД.
Оба адреса открыты в robots.txt и работают без входа. Заголовок `Prefer` нужен самому API:
без него сервер не находит процедуру и отвечает ошибкой 400.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from pif.adapters.base import CollectContext
from pif.adapters.generic import GenericAdapter, Link, link_report_date
from pif.errors import ParseError, UnknownFormatError
from pif.parsers.series import SeriesRow

API = "https://april-capital.ru/api/"
MOSCOW = timezone(timedelta(hours=3))
KOPECKS = Decimal(100)
MAX_POINTS = 1000
HEADERS = {"Prefer": "params=single-object"}
# Идентификатор фонда в API и код его раздела КИД на сайте. Связи между ними API не отдаёт.
KID_SECTIONS = {2: "APA", 3: "SB", 5: "GOSOBL", 6: "APASK", 7: "ANK", 16: "PDS"}


def _stamp(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=MOSCOW).astimezone(UTC).timestamp())


def price_rows(item: dict[str, Any], source: str) -> list[SeriesRow]:
    rows: list[SeriesRow] = []
    for point in item.get("priceList") or []:
        try:
            day = datetime.fromtimestamp(point["date"], MOSCOW).date()
            rows.append(SeriesRow(day, Decimal(point["sharePrice"]) / KOPECKS, Decimal(point["assetValue"]) / KOPECKS, "RUB"))
        except (KeyError, TypeError, ArithmeticError) as exc:
            raise UnknownFormatError(source, f"точка ряда без даты, пая или СЧА: {point!r}"[:160]) from exc
    return rows


def kid_links(listing: dict[str, Any], page: str) -> list[Link]:
    return [
        Link(f"{API}v01_download?code=SITE&id={item['id']}", item["title"], page)
        for section in (listing.get("success") or {}).values()
        for item in section.get("children", [])
    ]


class AprilAdapter(GenericAdapter):
    async def collect(self, ctx: CollectContext) -> None:
        body = {"pointsCount": MAX_POINTS, "dateStart": _stamp(ctx.cutoff), "dateEnd": _stamp(date.today())}
        details = await ctx.fetch(
            f"{API}v01_fund_details_list", method="POST", data=body, as_json=True, headers=HEADERS
        )
        ctx.counters["pages"] += 1
        for item in json.loads(details.content).get("success") or []:
            if not item.get("active"):
                continue
            fund = ctx.matcher.match(item["shortName"])
            if fund is None:
                ctx.note_unmatched(details.url, item["shortName"])
                continue
            try:
                ctx.add_series(fund, price_rows(item, details.url), details)
            except ParseError as exc:
                ctx.note_parse_error(details.url, exc, fund)
            section = KID_SECTIONS.get(item["id"])
            if section is None:
                ctx.note_unmatched(details.url, f"Нет раздела КИД: {item['shortName']}")
                continue
            listing = await ctx.try_fetch(
                f"{API}v01_files_list", params={"codeList": f"MAIN_INFO_DOC_{section}_CURRENT"}, fund=fund
            )
            if listing is None:
                continue
            for link in kid_links(json.loads(listing.content), listing.url):
                day = link_report_date(link.text, link.url)
                if day is None or day >= ctx.cutoff:
                    await self.collect_kid(ctx, link, fund=fund)
