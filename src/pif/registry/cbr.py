"""Реестр ПИФ Банка России (list_pif.xlsx) и карточки участников финансового рынка."""

from __future__ import annotations

import html
import io
import re
import warnings
from dataclasses import dataclass
from datetime import date

import openpyxl

from pif.errors import UnknownFormatError
from pif.models import FundType
from pif.parsers.numbers import parse_date

CBR_REGISTRY_URL = "https://www.cbr.ru/vfs/finmarkets/files/supervision/list_pif.xlsx"
CBR_CARD_URL = "https://www.cbr.ru/finorg/foinfo/"

_REQUIRED_HEADERS = {
    "type": "Тип",
    "status": "Статус",
    "name": "Полное название",
    "short_name": "Краткое название",
    "category": "Категория",
    "rule_number": "Номер правил доверительного управления",
    "registered_on": "Дата присвоения номера",
    "formed_on": "Дата окончания формирования",
    "qualified": "Инвестиционные паи предназначены для квалифиц",
    "exchange": "Инвестиционные паи могут обращаться на организ",
    "previous_names": "Все предыдущие названия",
    "uk_name": "Полное фирменное наименование",
    "uk_ogrn": "ОГРН",
    "excluded_on": "Дата исключения",
}
_TYPES = {t.value: t for t in FundType}
_EXCLUDED = "Исключён из реестра"
_AS_OF = re.compile(r"по состоянию на (\d{2}\.\d{2}\.\d{4})")


@dataclass(frozen=True, slots=True)
class CbrFundRow:
    rule_number: str
    name: str
    short_name: str
    fund_type: FundType
    category: str
    status: str
    registered_on: date | None
    formed_on: date | None
    for_qualified: bool
    exchange_traded: bool
    previous_names: tuple[str, ...]
    uk_name: str
    uk_ogrn: str


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def _header_map(header: tuple[object, ...]) -> dict[str, int]:
    columns: dict[str, int] = {}
    cells = [_clean(c).lower() for c in header]
    for key, title in _REQUIRED_HEADERS.items():
        needle = title.lower()
        # «ОГРН» встречается в блоках УК, спецдепозитария и регистратора: нужен первый.
        index = next((i for i, cell in enumerate(cells) if cell.startswith(needle)), None)
        if index is None:
            raise UnknownFormatError("list_pif.xlsx", f"нет колонки «{title}»")
        columns[key] = index
    return columns


def parse_registry(content: bytes) -> tuple[date, list[CbrFundRow]]:
    """Возвращает дату реестра и действующие открытые, интервальные и биржевые фонды."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    rows = book.active.iter_rows(values_only=True)
    title = _clean(next(rows)[0])
    match = _AS_OF.search(title)
    if not match:
        raise UnknownFormatError("list_pif.xlsx", f"нет даты реестра в заголовке: {title[:80]!r}")
    as_of = parse_date(match[1])
    assert as_of is not None

    next(rows)  # групповая шапка
    columns = _header_map(next(rows))
    funds: list[CbrFundRow] = []
    for row in rows:
        fund_type = _TYPES.get(_clean(row[columns["type"]]))
        status = _clean(row[columns["status"]])
        if fund_type is None or status == _EXCLUDED or row[columns["excluded_on"]]:
            continue
        previous = _clean(row[columns["previous_names"]])
        funds.append(
            CbrFundRow(
                rule_number=_clean(row[columns["rule_number"]]),
                name=_clean(row[columns["name"]]),
                short_name=_clean(row[columns["short_name"]]),
                fund_type=fund_type,
                category=_clean(row[columns["category"]]),
                status=status,
                registered_on=parse_date(row[columns["registered_on"]]),
                formed_on=parse_date(row[columns["formed_on"]]),
                for_qualified=_clean(row[columns["qualified"]]).lower() == "да",
                exchange_traded=_clean(row[columns["exchange"]]).lower() == "да",
                previous_names=tuple(p.strip() for p in re.split(r"[;\n]", previous) if p.strip()),
                uk_name=_clean(row[columns["uk_name"]]),
                uk_ogrn=_clean(row[columns["uk_ogrn"]]),
            )
        )
    if not funds:
        raise UnknownFormatError("list_pif.xlsx", "ни одной строки с действующими фондами")
    return as_of, funds


@dataclass(frozen=True, slots=True)
class CbrCard:
    ogrn: str
    inn: str
    short_name: str
    full_name: str
    site: str | None


def parse_card(ogrn: str, page: str) -> CbrCard:
    """Разбирает карточку участника рынка на cbr.ru. ИНН в реестре ПИФ нет, он есть здесь."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S)
    text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)))

    def pick(pattern: str) -> str | None:
        found = re.search(pattern, text)
        return found[1].strip() if found else None

    inn = pick(r"ИНН (\d{10,12})")
    short_name = pick(r"Сокращенное \(фирменное\) наименование (.+?) ИНН")
    if not inn or not short_name:
        raise UnknownFormatError(f"карточка ЦБ {ogrn}", "нет ИНН или названия")
    site = pick(r"Интернет-ресурсы (\S+)")
    return CbrCard(
        ogrn=ogrn,
        inn=inn,
        short_name=short_name,
        full_name=pick(r"Полное \(фирменное\) наименование (.+?) Сокращенное") or short_name,
        site=site if site and "." in site else None,
    )
