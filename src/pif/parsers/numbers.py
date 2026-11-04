"""Разбор чисел и дат в том виде, в каком их публикуют УК.

Правило модуля: значение, которое нельзя разобрать однозначно, даёт None или ошибку.
Подставлять ноль нельзя.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}  # fmt: skip
_SPACES = re.compile(r"[\s   ]+")
_CURRENCY_MARKS = re.compile(r"(?i)(₽|руб(\.|лей|ля|ль)?|rub|usd|eur|cny|\$|€|¥|%)")
_RU_DATE = re.compile(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", re.I)
_DOT_DATE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})\b")
_ISO_DATE = re.compile(r"\b(\d{4})[-.](\d{2})[-.](\d{2})")


def parse_decimal(raw: object) -> Decimal | None:
    """Разбирает '1 234 567,89 ₽', '1,234,567.89', 1234.5. Неоднозначное значение даёт None."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | Decimal):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(repr(raw))
    text = _CURRENCY_MARKS.sub("", _SPACES.sub("", str(raw))).strip()
    text = text.replace("−", "-").replace("–", "-").removeprefix("+")
    if not text or text in {"-", "—", "н/д", "нд", "n/a"}:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif text.count(",") == 1:
        text = text.replace(",", ".")
    elif text.count(",") > 1:
        text = text.replace(",", "")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date(raw: object) -> date | None:
    """Разбирает 31.01.2025, 31/01/25, 2025-01-31, «31 января 2025 г.»."""
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if raw is None:
        return None
    text = str(raw).strip()
    if match := _ISO_DATE.search(text):
        return _safe_date(int(match[1]), int(match[2]), int(match[3]))
    if match := _DOT_DATE.search(text):
        year = int(match[3])
        year += 2000 if year < 100 else 0
        return _safe_date(year, int(match[2]), int(match[1]))
    if match := _RU_DATE.search(text):
        name = match[2].lower()
        month = next((n for stem, n in _MONTHS.items() if name.startswith(stem)), None)
        if month:
            return _safe_date(int(match[3]), month, int(match[1]))
    return None


def parse_percent(raw: object) -> Decimal | None:
    return parse_decimal(str(raw).replace("%", "")) if raw is not None else None
