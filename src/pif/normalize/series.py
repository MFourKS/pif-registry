"""Нормализация рядов: одна непрерывная история на фонд без ложных скачков.

Правила:
* опубликованные значения не теряются: исправленный пай лежит рядом с исходным;
* каждое исправление попадает в журнал с причиной;
* недостающие точки не придумываются, пропуск остаётся пропуском;
* подозрительная точка без однозначного объяснения не меняется, а только помечается.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from pif.models import Observation
from pif.normalize.fx import FxRates

# Пай изменился больше чем в 1,5 раза за шаг, а СЧА почти нет: это дробление или консолидация паёв.
SPLIT_PRICE_LOG = math.log(1.5)
SPLIT_NAV_LOG = math.log(1.25)
# Значение ушло больше чем на 25 % и сразу вернулось: это опечатка источника.
SPIKE_LOG = math.log(1.25)
SPIKE_RETURN_LOG = math.log(1.08)
_SCALES = (Decimal(1000), Decimal(10**6))
_SCALE_TOLERANCE = 0.2
DAILY_GAP_DAYS = 10
MONTHLY_GAP_DAYS = 45


@dataclass(frozen=True, slots=True)
class Correction:
    rule_number: str
    day: date
    field: str
    old_value: str
    new_value: str
    reason: str
    source_id: str


@dataclass(slots=True)
class Point:
    day: date
    nav: Decimal | None
    unit_price: Decimal | None  # как опубликовано
    adj_price: Decimal | None  # сопоставимый с текущим паем после дроблений
    currency: str
    kind: str
    source_id: str
    nav_rub: Decimal | None = None
    price_rub: Decimal | None = None
    flag: str = ""


@dataclass(frozen=True, slots=True)
class Gap:
    start: date
    end: date
    days: int


@dataclass(slots=True)
class NormalizedSeries:
    rule_number: str
    points: list[Point] = field(default_factory=list)
    corrections: list[Correction] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    frequency: str = "none"
    currency: str = "RUB"


def _ratio_log(a: Decimal, b: Decimal) -> float:
    return math.log(float(a) / float(b))


def _dominant_currency(observations: Iterable[Observation]) -> str:
    counts = Counter(o.currency for o in observations if o.currency)
    return counts.most_common(1)[0][0] if counts else "RUB"


def _drop_invalid(rule: str, points: list[Point], corrections: list[Correction]) -> list[Point]:
    kept: list[Point] = []
    for point in points:
        for name in ("nav", "unit_price"):
            value = getattr(point, name)
            if value is not None and value <= 0:
                corrections.append(
                    Correction(rule, point.day, name, str(value), "", "Неположительное значение убрано", point.source_id)
                )
                setattr(point, name, None)
        point.adj_price = point.unit_price
        if point.nav is not None or point.unit_price is not None:
            kept.append(point)
    return kept


def _fix_nav_scale(rule: str, points: list[Point], corrections: list[Correction]) -> None:
    """СЧА в тысячах или миллионах рядом с рублями при неизменном пае: это ошибка единиц в источнике."""
    with_nav = [p for p in points if p.nav is not None]
    if len(with_nav) < 3:
        return
    ordered = sorted(float(p.nav) for p in with_nav)
    median = ordered[len(ordered) // 2]
    for point in with_nav:
        for scale in _SCALES:
            if abs(math.log(float(point.nav) * float(scale) / median)) < _SCALE_TOLERANCE:
                fixed = point.nav * scale
                corrections.append(
                    Correction(
                        rule,
                        point.day,
                        "nav",
                        str(point.nav),
                        str(fixed),
                        f"СЧА опубликована в других единицах: умножено на {scale}",
                        point.source_id,
                    )
                )
                point.nav = fixed
                break


def _flag_spikes(rule: str, points: list[Point], corrections: list[Correction]) -> None:
    """Одиночный выброс пая. Точка остаётся в данных, но не участвует в расчётах."""
    priced = [p for p in points if p.unit_price is not None]
    for before, current, after in zip(priced, priced[1:], priced[2:], strict=False):
        jump = _ratio_log(current.unit_price, before.unit_price)
        back = _ratio_log(after.unit_price, before.unit_price)
        if abs(jump) > SPIKE_LOG and abs(back) < SPIKE_RETURN_LOG:
            nav_ok = current.nav is None or before.nav is None or abs(_ratio_log(current.nav, before.nav)) < SPIKE_LOG
            if nav_ok:
                current.flag = "spike"
                current.adj_price = None
                corrections.append(
                    Correction(
                        rule,
                        current.day,
                        "unit_price",
                        str(current.unit_price),
                        "",
                        "Одиночный выброс пая при стабильных соседних точках: исключён из расчётов",
                        current.source_id,
                    )
                )


def _adjust_splits(rule: str, points: list[Point], corrections: list[Correction]) -> None:
    """Дробление паёв: приводит прошлые значения к текущему масштабу пая."""
    priced = [p for p in points if p.adj_price is not None]
    for index in range(len(priced) - 1, 0, -1):
        previous, current = priced[index - 1], priced[index]
        price_log = _ratio_log(current.adj_price, previous.adj_price)
        if abs(price_log) < SPLIT_PRICE_LOG:
            continue
        if previous.nav is None or current.nav is None:
            continue
        nav_log = _ratio_log(current.nav, previous.nav)
        if abs(nav_log) > SPLIT_NAV_LOG:
            continue
        # Доходность шага считаем равной изменению СЧА: притоки за один шаг несравнимо меньше дробления.
        factor = current.adj_price / previous.adj_price / (current.nav / previous.nav)
        for point in priced[:index]:
            point.adj_price = point.adj_price * factor
        corrections.append(
            Correction(
                rule,
                current.day,
                "adj_price",
                str(previous.unit_price),
                str(previous.adj_price),
                f"Дробление или консолидация паёв: прошлые значения умножены на {factor:.6f}",
                current.source_id,
            )
        )


def _frequency(points: list[Point]) -> str:
    if len(points) < 2:
        return "single" if points else "none"
    steps = sorted((b.day - a.day).days for a, b in zip(points, points[1:], strict=False))
    median = steps[len(steps) // 2]
    if median <= 4:
        return "daily"
    if median <= 10:
        return "weekly"
    return "monthly"


def _find_gaps(points: list[Point], frequency: str) -> list[Gap]:
    limit = DAILY_GAP_DAYS if frequency in {"daily", "weekly"} else MONTHLY_GAP_DAYS
    gaps = []
    for left, right in zip(points, points[1:], strict=False):
        days = (right.day - left.day).days
        if days > limit:
            gaps.append(Gap(left.day, right.day, days))
    return gaps


def normalize(
    rule_number: str,
    observations: dict[date, Observation],
    fx: FxRates,
    not_before: date | None = None,
) -> NormalizedSeries:
    result = NormalizedSeries(rule_number)
    ordered = [observations[d] for d in sorted(observations) if not_before is None or d >= not_before]
    result.currency = _dominant_currency(ordered)
    points = [
        Point(o.day, o.nav, o.unit_price, o.unit_price, o.currency or result.currency, o.kind, o.source_id)
        for o in ordered
    ]
    corrections = result.corrections
    points = _drop_invalid(rule_number, points, corrections)
    _fix_nav_scale(rule_number, points, corrections)
    _flag_spikes(rule_number, points, corrections)
    _adjust_splits(rule_number, points, corrections)

    for point in points:
        rate = fx.rate(point.currency, point.day)
        if rate is None:
            continue
        point.nav_rub = None if point.nav is None else point.nav * rate
        point.price_rub = None if point.unit_price is None else point.unit_price * rate

    result.points = points
    result.frequency = _frequency(points)
    result.gaps = _find_gaps(points, result.frequency)
    return result


def month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def month_end_points(points: list[Point]) -> dict[str, Point]:
    """Последняя опубликованная точка месяца, где есть и СЧА, и сопоставимая стоимость пая."""
    result: dict[str, Point] = {}
    for point in points:
        if point.nav is None or point.adj_price is None:
            continue
        key = month_key(point.day)
        if key not in result or point.day > result[key].day:
            result[key] = point
    return result


def last_month_end(as_of: date) -> date:
    return as_of.replace(day=1) - timedelta(days=1)
