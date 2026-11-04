"""Разборщик справки о стоимости чистых активов (форма 0420502).

Это отчётная форма Банка России с номерами строк: 14 — СЧА, 16 — расчётная стоимость пая.
В разделе 1 указаны номер правил и ISIN, поэтому документ сам называет свой фонд.
УК публикуют справку в PDF и XLSX, сводим оба случая к тексту.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import openpyxl

from pif.errors import UnknownFormatError
from pif.matching import rule_numbers_in
from pif.parsers.kid import extract_text
from pif.parsers.numbers import parse_date, parse_decimal

_FORM_MARK = re.compile(r"(?i)0420502|справк\w+\s+о\s+стоимости\s+чистых\s+активов")
_AS_OF = re.compile(r"(?is)по\s+состоянию\s+на\s+(\d{1,2}[./]\d{1,2}[./]\d{2,4}|\d{1,2}\s+[а-яё]+\s+\d{4})")
_AMOUNT = r"(\d[\d  ]*[.,]\d+)"
_NAV_ROW = re.compile(rf"(?i)стоимост\w*\s+чистых\s+активов[ \t]+14[ \t]+{_AMOUNT}")
_NAV_ROW_LOOSE = re.compile(rf"(?m)^\s*(?:\S+\s+){{0,12}}?14[ \t]+{_AMOUNT}")
_PRICE_ROW = re.compile(rf"(?m)(?:^|[ \t])16[ \t]+{_AMOUNT}")
_REPORT_DATE = re.compile(r"(?is)отч[её]тная\s+дата.{0,160}?(\d{2}\.\d{2}\.20\d{2}|20\d{2}[.-]\d{2}[.-]\d{2})")
_ISIN = re.compile(r"\bRU[0-9A-Z]{10}\b")
_FOUR_DIGIT_RULE = re.compile(r"(?i)(?:№|номер\w*)[^\n]{0,60}?\b(\d{4})\b")


@dataclass(frozen=True, slots=True)
class NavStatement:
    report_date: date
    nav: Decimal
    unit_price: Decimal
    rule_numbers: frozenset[str]
    isin: str | None
    text_head: str


def _xlsx_text(content: bytes) -> str:
    book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    lines: list[str] = []
    for sheet in book.worksheets:
        for row in sheet.iter_rows(values_only=True):
            cells = [_cell(c) for c in row if c is not None and str(c).strip()]
            if cells:
                lines.append(" ".join(cells))
    return "\n".join(lines)


def _cell(value: object) -> str:
    if isinstance(value, float | int) and not isinstance(value, bool):
        # Числа из Excel приводим к виду с дробной частью, как в PDF.
        return f"{value:.4f}" if isinstance(value, float) or value > 100 else str(value)
    return re.sub(r"\s+", " ", str(value)).strip()


def parse_nav_statement(content: bytes, fmt: str, source: str = "справка СЧА") -> NavStatement:
    text = _xlsx_text(content) if fmt == "xlsx" else extract_text(content, fmt)
    if not _FORM_MARK.search(text):
        raise UnknownFormatError(source, "документ не похож на справку о СЧА")
    date_match = _AS_OF.search(text) or _REPORT_DATE.search(text)
    day = parse_date(date_match[1]) if date_match else None
    if day is None:
        raise UnknownFormatError(source, "в справке нет даты «по состоянию на»")
    nav_match = _NAV_ROW.search(text) or _NAV_ROW_LOOSE.search(text)
    price_match = _PRICE_ROW.search(text)
    nav = parse_decimal(nav_match[1]) if nav_match else None
    price = parse_decimal(price_match[1]) if price_match else None
    if nav is None or price is None:
        raise UnknownFormatError(source, "нет строки 14 (СЧА) или строки 16 (стоимость пая)")
    rules = set(rule_numbers_in(text)) | set(_FOUR_DIGIT_RULE.findall(text[:3000]))
    isin = _ISIN.search(text)
    return NavStatement(day, nav, price, frozenset(rules), isin[0] if isin else None, text[:1500])
