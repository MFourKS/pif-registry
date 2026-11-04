"""Поиск рядов, встроенных в HTML-страницы УК.

Сайты отдают ряды для графиков прямо в разметке: в атрибутах, переменных и состоянии SSR.
Здесь собраны распознаватели таких блоков. Каждый проверен на настоящей странице (см. tests).
"""

from __future__ import annotations

import html as html_lib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from pif.errors import UnknownFormatError
from pif.parsers.numbers import parse_date, parse_decimal
from pif.parsers.series import (
    SeriesRow,
    balanced_json,
    html_table,
    json_date_map,
    json_records,
    merge_rows,
)

_RECORD_ARRAY = re.compile(r'\[\s*\{\s*"(?:date|dt|Date|operDate|OperDate|day)"\s*:')
_DATE_MAP = re.compile(r'\{\s*"\d{2}\.\d{2}\.\d{4}"\s*:')
MIN_POINTS = 3


def first_am_chart(page: str, source: str) -> list[SeriesRow]:
    """УК Первая: атрибут chart-fund-data с рядами стоимости пая и СЧА."""
    found = re.search(r"chart-fund-data='([^']+)'", page)
    if not found:
        return []
    payload = json.loads(html_lib.unescape(found[1]))
    rows: list[SeriesRow] = []
    for item in payload.get("chartData", []):
        if item.get("type") != "fund":
            continue
        day = parse_date(item.get("date"))
        # price на сайте пересчитан с учётом дробления паёв, originalPrice — как было опубликовано.
        value = parse_decimal(item.get("originalPrice", item.get("price")))
        if day and value is not None:
            rows.append(SeriesRow(day, value, parse_decimal(item.get("net_assets")), payload.get("currencyCode")))
    if payload.get("chartData") and not rows:
        keys = sorted(payload["chartData"][0])
        raise UnknownFormatError(source, f"chart-fund-data без значений пая, ключи {keys}")
    return rows


def js_graph_variable(page: str, source: str) -> list[SeriesRow]:
    """Переменная `var graph = {"data": {"31.10.2026": 123.45}}` на сайтах на Битриксе."""
    found = re.search(r"var\s+graph\s*=\s*", page)
    if not found:
        return []
    payload = balanced_json(page, found.end())
    data = payload.get("data") if isinstance(payload, Mapping) else None
    return json_date_map(data, source) if isinstance(data, Mapping) else []


def record_arrays(page: str, source: str) -> list[SeriesRow]:
    """Массивы вида [{"date": ..., "share": ..., "scha": ...}] в любом месте разметки."""
    text = html_lib.unescape(page)
    best: list[SeriesRow] = []
    for match in _RECORD_ARRAY.finditer(text):
        try:
            records = balanced_json(text, match.start())
        except json.JSONDecodeError:
            continue
        if not isinstance(records, list) or len(records) < MIN_POINTS:
            continue
        try:
            rows = json_records(records, source)
        except UnknownFormatError:
            continue
        if len(rows) > len(best):
            best = rows
    return best


def nested_date_map(page: str, source: str) -> list[SeriesRow]:
    """Экранированный словарь {"16.11.2022": {"nav": ..., "piePrice": ...}} из SSR-состояния."""
    text = page.replace('\\"', '"')
    best: list[SeriesRow] = []
    for match in _DATE_MAP.finditer(text):
        try:
            mapping = balanced_json(text, match.start())
        except json.JSONDecodeError:
            continue
        if not isinstance(mapping, Mapping) or len(mapping) < MIN_POINTS:
            continue
        first = next(iter(mapping.values()))
        if not isinstance(first, Mapping):
            continue
        rows = json_date_map(mapping, source)
        if len(rows) > len(best):
            best = rows
    return best


def labelled_arrays(page: str, source: str) -> list[SeriesRow]:
    """График Chart.js: labels: ["18.10.2023", ...] и data: [1.2, ...] рядом друг с другом."""
    found = re.search(r'(?:labels|categories)\s*["\']?\s*:\s*(\[\s*["\']\d{2}\.\d{2}\.\d{4}["\'])', page)
    if not found:
        return []
    labels = balanced_json(page.replace("'", '"'), found.start(1))
    tail = page[found.end() : found.end() + 40 * len(labels) + 4000]
    values = re.search(r'data["\']?\s*:\s*(\[[\d.,\s"\'-]+\])', tail)
    if not values:
        return []
    numbers = json.loads(values[1].replace("'", '"'))
    if len(numbers) != len(labels):
        raise UnknownFormatError(source, f"длины рядов графика различаются: {len(labels)} и {len(numbers)}")
    rows = []
    for label, raw in zip(labels, numbers, strict=True):
        day, value = parse_date(label), parse_decimal(raw)
        if day and value is not None:
            rows.append(SeriesRow(day, value, None))
    return rows


def nuxt_payload(page: str, source: str) -> list[SeriesRow]:
    """Состояние Nuxt: плоский массив, где поля записи хранят индексы значений (Солид Менеджмент)."""
    found = re.search(r'id="__NUXT_DATA__"[^>]*>(\[.*?\])</script>', page, re.S)
    if not found:
        return []
    payload = json.loads(found[1])
    rows: list[SeriesRow] = []
    for item in payload:
        if isinstance(item, dict) and {"OperDate", "Price", "Value"} <= item.keys():
            day = parse_date(payload[item["OperDate"]])
            price, nav = parse_decimal(payload[item["Price"]]), parse_decimal(payload[item["Value"]])
            if day and price:
                rows.append(SeriesRow(day, price, nav if nav else None))
    return rows


def plain_table(page: str, source: str) -> list[SeriesRow]:
    if "<table" not in page:
        return []
    try:
        return html_table(page, source)
    except UnknownFormatError:
        return []


RECOGNIZERS: tuple[tuple[str, Callable[[str, str], list[SeriesRow]]], ...] = (
    ("first_am_chart", first_am_chart),
    ("js_graph_variable", js_graph_variable),
    ("record_arrays", record_arrays),
    ("nested_date_map", nested_date_map),
    ("labelled_arrays", labelled_arrays),
    ("nuxt_payload", nuxt_payload),
    ("html_table", plain_table),
)


def extract_series(page: str, source: str) -> tuple[str, list[SeriesRow]] | None:
    """Прогоняет страницу через распознаватели и объединяет всё найденное.

    Часто пай и СЧА лежат в разных блоках страницы, поэтому первый успех не останавливает поиск.
    """
    parts: list[list[SeriesRow]] = []
    names: list[str] = []
    for name, recognizer in RECOGNIZERS:
        try:
            rows: Any = recognizer(page, source)
        except json.JSONDecodeError:
            continue
        if rows and len(rows) >= MIN_POINTS:
            parts.append(rows)
            names.append(name)
    if not parts:
        return None
    return "+".join(names), merge_rows(*parts)
