"""Этап реестра: ЦБ + Мосбиржа + карточки УК + ссылки на раздел «Сравнить УК»."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from pif.config import Settings
from pif.errors import FetchError
from pif.models import Fund, FundType, ManagementCompany
from pif.net.client import Fetcher
from pif.observability import get_logger, tracer
from pif.registry import cbr, moex
from pif.registry.classify import asset_class

log = get_logger("registry")

SRAVNENIE_DATA_URL = "https://algosphere.tech/sravnenie-data?v=names3"
_SRAVNENIE_ROW = re.compile(r'href="/uk/(\d+)".*?<td[^>]*>\s*(\d{10,12})\s*</td>', re.S)


@dataclass(slots=True)
class Registry:
    as_of: date
    source_url: str
    source_id: str
    funds: list[Fund]
    companies: dict[str, ManagementCompany]

    def funds_of(self, inn: str) -> list[Fund]:
        return [f for f in self.funds if f.uk_inn == inn]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "as_of": self.as_of.isoformat(),
            "source_url": self.source_url,
            "source_id": self.source_id,
            "funds": [f.to_json() for f in sorted(self.funds, key=lambda f: f.rule_number)],
            "companies": [asdict(c) for c in sorted(self.companies.values(), key=lambda c: c.inn)],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Registry:
        raw = json.loads(path.read_text(encoding="utf-8"))
        funds = []
        for stored in raw["funds"]:
            item = dict(stored)
            item["fund_type"] = FundType(item["fund_type"])
            for key in ("registered_on", "formed_on"):
                item[key] = date.fromisoformat(item[key]) if item[key] else None
            funds.append(Fund(**item))
        companies = {c["inn"]: ManagementCompany(**c) for c in raw["companies"]}
        return cls(date.fromisoformat(raw["as_of"]), raw["source_url"], raw["source_id"], funds, companies)


def parse_sravnenie_ids(page: str) -> dict[str, str]:
    return {inn: uk_id for uk_id, inn in _SRAVNENIE_ROW.findall(page)}


async def build_registry(settings: Settings, fetcher: Fetcher) -> Registry:
    with tracer().start_as_current_span("registry.build") as span:
        source = await fetcher.get(cbr.CBR_REGISTRY_URL)
        as_of, rows = cbr.parse_registry(source.content)
        log.info("cbr_registry_parsed", as_of=as_of.isoformat(), funds=len(rows))

        cards: dict[str, cbr.CbrCard] = {}
        for ogrn in sorted({r.uk_ogrn for r in rows}):
            page = await fetcher.get(cbr.CBR_CARD_URL, params={"ogrn": ogrn}, ttl=7 * 86400)
            cards[ogrn] = cbr.parse_card(ogrn, page.text)

        securities = moex.index_by_rule(await moex.fetch_securities(fetcher))

        try:
            sravnenie = parse_sravnenie_ids((await fetcher.get(SRAVNENIE_DATA_URL, ttl=7 * 86400)).text)
        except FetchError as exc:
            log.warning("sravnenie_unavailable", error=str(exc))
            sravnenie = {}

        funds: list[Fund] = []
        for row in rows:
            card = cards[row.uk_ogrn]
            sec = moex.lookup(securities, row.rule_number)
            if sec is not None and sec.emitent_inn and sec.emitent_inn != card.inn:
                # Часть номера совпала случайно или биржа ещё не отразила смену УК.
                log.info("moex_emitent_mismatch", rule=row.rule_number, secid=sec.secid)
                sec = None if row.rule_number.split("-")[0] == sec.regnumber else sec
            funds.append(
                Fund(
                    rule_number=row.rule_number,
                    name=row.name,
                    short_name=row.short_name or row.name,
                    fund_type=row.fund_type,
                    cbr_category=row.category,
                    asset_class=asset_class(row.name),
                    uk_inn=card.inn,
                    uk_ogrn=card.ogrn,
                    uk_name=card.short_name,
                    registered_on=row.registered_on,
                    formed_on=row.formed_on,
                    cbr_status=row.status,
                    for_qualified=row.for_qualified,
                    exchange_traded=row.exchange_traded,
                    previous_names=list(row.previous_names),
                    isin=sec.isin if sec else None,
                    ticker=sec.secid if sec else None,
                )
            )
        companies = {
            card.inn: ManagementCompany(
                inn=card.inn,
                ogrn=card.ogrn,
                name=card.full_name,
                short_name=card.short_name,
                site=card.site,
                sravnenie_id=sravnenie.get(card.inn),
            )
            for card in cards.values()
        }
        span.set_attribute("pif.funds", len(funds))
        span.set_attribute("pif.companies", len(companies))
        log.info(
            "registry_built",
            funds=len(funds),
            companies=len(companies),
            with_isin=sum(1 for f in funds if f.isin),
            with_sravnenie=sum(1 for c in companies.values() if c.sravnenie_id),
        )
        return Registry(as_of, cbr.CBR_REGISTRY_URL, source.source_id, funds, companies)
