"""Мосбиржа ISS: ISIN и биржевые тикеры паёв. Официальный источник организатора торгов."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pif.net.client import Fetcher

ISS_SECURITIES = "https://iss.moex.com/iss/securities.json"
PAGE = 100


@dataclass(frozen=True, slots=True)
class MoexSecurity:
    secid: str
    isin: str
    regnumber: str
    name: str
    emitent_inn: str | None
    sec_type: str
    is_traded: bool


def parse_page(content: bytes) -> list[MoexSecurity]:
    block = json.loads(content)["securities"]
    columns = block["columns"]
    result = []
    for row in block["data"]:
        item = dict(zip(columns, row, strict=True))
        if not item.get("regnumber") or not item.get("isin"):
            continue
        result.append(
            MoexSecurity(
                secid=item["secid"],
                isin=item["isin"],
                regnumber=str(item["regnumber"]).strip(),
                name=item.get("name") or "",
                emitent_inn=item.get("emitent_inn"),
                sec_type=item.get("type") or "",
                is_traded=bool(item.get("is_traded")),
            )
        )
    return result


async def fetch_securities(fetcher: Fetcher) -> list[MoexSecurity]:
    securities: list[MoexSecurity] = []
    start = 0
    while True:
        page = await fetcher.get(
            ISS_SECURITIES,
            params={
                "group_by": "group",
                "group_by_filter": "stock_ppif",
                "limit": PAGE,
                "start": start,
                "iss.meta": "off",
            },
            documented_api=True,
        )
        rows = json.loads(page.content)["securities"]["data"]
        if not rows:
            return securities
        securities.extend(parse_page(page.content))
        start += PAGE


def index_by_rule(securities: list[MoexSecurity]) -> dict[str, MoexSecurity]:
    """Ключ: номер правил. У биржи в regnumber лежит либо он целиком, либо первая часть."""
    index: dict[str, MoexSecurity] = {}
    for sec in securities:
        if sec.sec_type not in {"public_ppif", "interval_ppif", "exchange_ppif"}:
            continue
        current = index.get(sec.regnumber)
        if current is None or (sec.is_traded and not current.is_traded):
            index[sec.regnumber] = sec
    return index


def lookup(index: dict[str, MoexSecurity], rule_number: str) -> MoexSecurity | None:
    return index.get(rule_number) or index.get(rule_number.split("-", maxsplit=1)[0])
