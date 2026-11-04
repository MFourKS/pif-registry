"""Разборщики рядов СЧА и стоимости пая.

На каждый вид источника свой разборщик, все возвращают список SeriesRow:
* json_records      — массив объектов с датой, паем и СЧА (встроенный JSON, API);
* json_date_map     — словарь {дата: значение} (графики на сайтах УК);
* html_table        — HTML-таблица с заголовками;
* delimited_text    — CSV и другие текстовые таблицы;
* spreadsheet       — XLSX и XLS.

Колонки определяются по названиям заголовков, а не по порядку. Если значимую колонку найти
нельзя, поднимается UnknownFormatError: лучше явная ошибка, чем ряд из нулей.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import lxml.html

from pif.errors import UnknownFormatError
from pif.parsers.numbers import parse_date, parse_decimal


@dataclass(frozen=True, slots=True)
class SeriesRow:
    day: date
    unit_price: Decimal | None
    nav: Decimal | None
    currency: str | None = None


_DATE_KEYS = ("date", "dt", "operdate", "calcdate", "tradedate", "x", "day", "reportdate")
_PRICE_KEYS = (
    "sharecost", "share", "piepice", "pieprice", "unitprice", "price", "pay", "pai", "rsp", "cost",
    "shareprice", "navpershare", "navperunit", "unit", "valuepay", "value_pai", "pricepie",
)  # fmt: skip
_NAV_KEYS = ("netassetvalue", "scha", "nav", "sca", "netassets", "assets", "valuescha", "value")

_HEADER_DATE = re.compile(r"(?i)\bдата\b|^date$")
_HEADER_PRICE = re.compile(r"(?i)стоимост\w*\s+(\w+\s+)?(инвестиционного\s+)?па[яй]|\bпа[яй]\b|\bрсп\b|цена\s+пая|unit|nav per")
_HEADER_NAV = re.compile(r"(?i)чистых\s+актив|\bсча\b|\bnav\b|net asset")
_HEADER_CHANGE = re.compile(r"(?i)измен|прирост|отклон|%|колич|выдан|погаш")
_CURRENCY_IN_HEADER = {"руб": "RUB", "₽": "RUB", "долл": "USD", "usd": "USD", "$": "USD", "евро": "EUR", "eur": "EUR", "юан": "CNY", "cny": "CNY"}


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-zа-я]", "", key.lower())


def _pick(item: Mapping[str, Any], names: Iterable[str]) -> Any:
    keys = {_norm_key(k): k for k in item}
    for name in names:
        if name in keys:
            return item[keys[name]]
    return None


def json_records(records: Sequence[Mapping[str, Any]], source: str) -> list[SeriesRow]:
    rows: list[SeriesRow] = []
    for item in records:
        day = parse_date(_pick(item, _DATE_KEYS))
        if day is None:
            continue
        price = parse_decimal(_pick(item, _PRICE_KEYS))
        nav = parse_decimal(_pick(item, _NAV_KEYS))
        if price is None and nav is None:
            continue
        currency = _pick(item, ("currency", "currencycode", "ccy"))
        rows.append(SeriesRow(day, price, nav, str(currency).upper() if currency else None))
    if records and not rows:
        raise UnknownFormatError(source, f"в записях нет даты и значений, ключи: {sorted(records[0])[:12]}")
    return rows


def json_date_map(mapping: Mapping[str, Any], source: str, *, field: str = "unit_price") -> list[SeriesRow]:
    rows: list[SeriesRow] = []
    for raw_day, raw_value in mapping.items():
        day = parse_date(raw_day)
        if day is None:
            continue
        if isinstance(raw_value, Mapping):
            rows.extend(json_records([{**raw_value, "date": raw_day}], source))
            continue
        value = parse_decimal(raw_value)
        if value is None:
            continue
        price, nav = (value, None) if field == "unit_price" else (None, value)
        rows.append(SeriesRow(day, price, nav))
    if mapping and not rows:
        raise UnknownFormatError(source, "словарь {дата: значение} не содержит ни одной точки")
    return rows


@dataclass(frozen=True, slots=True)
class _Columns:
    day: int
    price: int | None
    nav: int | None
    currency: str | None


def _locate_columns(header: Sequence[str]) -> _Columns | None:
    day = price = nav = None
    currency = None
    for index, raw in enumerate(header):
        cell = re.sub(r"\s+", " ", raw).strip()
        if not cell:
            continue
        if day is None and _HEADER_DATE.search(cell):
            day = index
            continue
        if _HEADER_CHANGE.search(cell):
            continue
        if nav is None and _HEADER_NAV.search(cell):
            nav = index
        elif price is None and _HEADER_PRICE.search(cell):
            price = index
        else:
            continue
        lowered = cell.lower()
        currency = currency or next((c for mark, c in _CURRENCY_IN_HEADER.items() if mark in lowered), None)
    if day is None or (price is None and nav is None):
        return None
    return _Columns(day, price, nav, currency)


def _rows_from_grid(grid: Iterable[Sequence[Any]], source: str) -> list[SeriesRow]:
    columns: _Columns | None = None
    header_seen: list[str] = []
    rows: list[SeriesRow] = []
    for line in grid:
        if columns is None:
            cells = ["" if c is None else str(c) for c in line]
            if any(cells) and len(header_seen) < 5:
                header_seen.append(" | ".join(c for c in cells if c)[:120])
            columns = _locate_columns(cells)
            continue
        if columns.day >= len(line):
            continue
        day = parse_date(line[columns.day])
        if day is None:
            continue

        def value(index: int | None, line: Sequence[Any] = line) -> Decimal | None:
            return parse_decimal(line[index]) if index is not None and index < len(line) else None

        price, nav = value(columns.price), value(columns.nav)
        if price is not None or nav is not None:
            rows.append(SeriesRow(day, price, nav, columns.currency))
    if columns is None:
        raise UnknownFormatError(source, f"нет заголовков с датой и стоимостью: {header_seen[:2]}")
    return rows


def html_table(page: str, source: str) -> list[SeriesRow]:
    """Находит на странице таблицу с датой и стоимостью. Берёт самую длинную из подходящих."""
    document = lxml.html.fromstring(page)
    best: list[SeriesRow] = []
    for table in document.iter("table"):
        grid = [
            [" ".join(cell.text_content().split()) for cell in row.iterchildren("td", "th")]
            for row in table.iter("tr")
        ]
        try:
            rows = _rows_from_grid(grid, source)
        except UnknownFormatError:
            continue
        if len(rows) > len(best):
            best = rows
    if not best:
        raise UnknownFormatError(source, "на странице нет таблицы с датой и стоимостью")
    return best


def delimited_text(text: str, source: str) -> list[SeriesRow]:
    sample = text[:4096]
    delimiter = max(";,\t|", key=sample.count)
    if sample.count(delimiter) == 0:
        raise UnknownFormatError(source, "в тексте нет разделителей колонок")
    return _rows_from_grid(csv.reader(io.StringIO(text), delimiter=delimiter), source)


def spreadsheet(content: bytes, fmt: str, source: str) -> list[SeriesRow]:
    grids: list[list[Sequence[Any]]] = []
    if fmt == "xlsx":
        import openpyxl

        book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        grids = [list(sheet.iter_rows(values_only=True)) for sheet in book.worksheets]
    elif fmt == "xls":
        import xlrd

        book = xlrd.open_workbook(file_contents=content)
        for sheet in book.sheets():
            grid: list[Sequence[Any]] = []
            for r in range(sheet.nrows):
                line: list[Any] = []
                for cell in sheet.row(r):
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        line.append(xlrd.xldate_as_datetime(cell.value, book.datemode))
                    else:
                        line.append(cell.value)
                grid.append(line)
            grids.append(grid)
    else:
        raise UnknownFormatError(source, f"таблица в формате {fmt} не поддержана")

    best: list[SeriesRow] = []
    last_error: UnknownFormatError | None = None
    for grid in grids:
        try:
            rows = _rows_from_grid(grid, source)
        except UnknownFormatError as exc:
            last_error = exc
            continue
        if len(rows) > len(best):
            best = rows
    if not best:
        raise last_error or UnknownFormatError(source, "в книге нет листов с данными")
    return best


def balanced_json(text: str, start: int) -> Any:
    """Читает JSON-значение, начинающееся в позиции start. Хвост страницы игнорируется."""
    return json.JSONDecoder().raw_decode(text, start)[0]


def merge_rows(*parts: Iterable[SeriesRow]) -> list[SeriesRow]:
    """Склеивает ряды пая и СЧА из разных блоков одной страницы в одну точку на дату."""
    merged: dict[date, SeriesRow] = {}
    for part in parts:
        for row in part:
            known = merged.get(row.day)
            merged[row.day] = (
                row
                if known is None
                else SeriesRow(
                    row.day,
                    known.unit_price if known.unit_price is not None else row.unit_price,
                    known.nav if known.nav is not None else row.nav,
                    known.currency or row.currency,
                )
            )
    return [merged[d] for d in sorted(merged)]
