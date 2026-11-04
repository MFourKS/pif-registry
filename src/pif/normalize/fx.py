"""Официальные курсы Банка России для перевода значений в рубли."""

from __future__ import annotations

import csv
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from pif.errors import UnknownFormatError
from pif.net.client import Fetcher
from pif.parsers.numbers import parse_date, parse_decimal

CBR_DYNAMIC_URL = "https://www.cbr.ru/scripts/XML_dynamic.asp"
CURRENCY_CODES = {"USD": "R01235", "EUR": "R01239", "CNY": "R01375"}
_RECORD = re.compile(r'<Record Date="([\d.]+)"[^>]*>.*?<Nominal>(\d+)</Nominal>.*?<Value>([\d,]+)</Value>', re.S)
# Выходные и праздники закрываем последним известным курсом, но не дальше этого окна.
MAX_LOOKBACK_DAYS = 10


class FxRates:
    def __init__(self, rates: dict[str, dict[date, Decimal]] | None = None) -> None:
        self.rates = rates or {}

    def rate(self, currency: str, day: date) -> Decimal | None:
        if currency == "RUB":
            return Decimal(1)
        table = self.rates.get(currency)
        if not table:
            return None
        for offset in range(MAX_LOOKBACK_DAYS + 1):
            value = table.get(day - timedelta(days=offset))
            if value is not None:
                return value
        return None

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        for currency, table in self.rates.items():
            with (directory / f"{currency}.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, lineterminator="\n")
                writer.writerow(["date", "rub_per_unit"])
                for day in sorted(table):
                    writer.writerow([day.isoformat(), str(table[day])])

    @classmethod
    def load(cls, directory: Path) -> FxRates:
        rates: dict[str, dict[date, Decimal]] = {}
        for path in directory.glob("*.csv") if directory.exists() else []:
            with path.open(encoding="utf-8", newline="") as handle:
                rates[path.stem] = {
                    date.fromisoformat(r["date"]): Decimal(r["rub_per_unit"]) for r in csv.DictReader(handle)
                }
        return cls(rates)


def parse_dynamic(xml_text: str, currency: str) -> dict[date, Decimal]:
    table: dict[date, Decimal] = {}
    for raw_day, nominal, value in _RECORD.findall(xml_text):
        day, rate = parse_date(raw_day), parse_decimal(value)
        if day and rate:
            table[day] = rate / Decimal(nominal)
    if not table:
        raise UnknownFormatError(f"курс {currency} ЦБ", "в ответе нет записей")
    return table


async def fetch_rates(fetcher: Fetcher, currencies: set[str], start: date, end: date) -> FxRates:
    rates: dict[str, dict[date, Decimal]] = {}
    for currency in sorted(currencies - {"RUB"}):
        code = CURRENCY_CODES.get(currency)
        if code is None:
            raise UnknownFormatError("валюта пая", f"нет кода ЦБ для {currency}")
        page = await fetcher.get(
            CBR_DYNAMIC_URL,
            params={
                "date_req1": start.strftime("%d/%m/%Y"),
                "date_req2": end.strftime("%d/%m/%Y"),
                "VAL_NM_RQ": code,
            },
        )
        rates[currency] = parse_dynamic(page.text, currency)
    return FxRates(rates)
