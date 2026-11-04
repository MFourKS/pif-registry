"""Самопроверка: доходность по собранному ряду против официальной из КИД."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from pif.models import OfficialReturn
from pif.normalize.series import NormalizedSeries, Point

PERIODS = (1, 3, 6, 12)
# Точка считается «на дату», если она не старше недели: праздники бывают длинными.
MAX_STALENESS_DAYS = 7
MONTH_END_WINDOW_DAYS = 4


@dataclass(frozen=True, slots=True)
class YieldCheck:
    rule_number: str
    report_date: date
    months: int
    official_pct: Decimal
    computed_pct: Decimal | None
    source_id: str
    start_day: date | None
    end_day: date | None

    @property
    def difference_pp(self) -> Decimal | None:
        if self.computed_pct is None:
            return None
        return self.computed_pct - self.official_pct


def shift_months(day: date, months: int) -> date:
    """Та же позиция в месяце N месяцев назад. Конец месяца остаётся концом месяца."""
    # Отчётная дата 30 октября при субботе 31-го: это тоже конец месяца, только последний рабочий день.
    is_month_end = (day + timedelta(days=MONTH_END_WINDOW_DAYS)).month != day.month
    index = day.year * 12 + day.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    first_next = date(year + (month == 12), month % 12 + 1, 1)
    last = first_next - timedelta(days=1)
    return last if is_month_end else date(year, month, min(day.day, last.day))


def point_on(points: list[Point], day: date) -> Point | None:
    """Последняя точка с паем не позже даты. Искать вперёд нельзя: это заглядывание в будущее."""
    found: Point | None = None
    for point in points:
        if point.day > day:
            break
        if point.adj_price is not None:
            found = point
    if found is None or (day - found.day).days > MAX_STALENESS_DAYS:
        return None
    return found


def computed_return(points: list[Point], end_day: date, months: int) -> tuple[Decimal | None, Point | None, Point | None]:
    end = point_on(points, end_day)
    if end is None:
        return None, None, None
    start = point_on(points, shift_months(end_day, months))
    if start is None:
        return None, None, end
    value = (end.adj_price / start.adj_price - 1) * 100
    return value, start, end


def check_fund(series: NormalizedSeries, official: list[OfficialReturn]) -> list[YieldCheck]:
    if not official:
        return []
    latest = max(r.report_date for r in official)
    checks: list[YieldCheck] = []
    for record in sorted(official, key=lambda r: r.months):
        if record.report_date != latest or record.months not in PERIODS:
            continue
        value, start, end = computed_return(series.points, record.report_date, record.months)
        checks.append(
            YieldCheck(
                rule_number=series.rule_number,
                report_date=record.report_date,
                months=record.months,
                official_pct=record.value_pct,
                computed_pct=value,
                source_id=record.source_id,
                start_day=start.day if start else None,
                end_day=end.day if end else None,
            )
        )
    return checks
