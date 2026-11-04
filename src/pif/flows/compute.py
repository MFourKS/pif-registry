"""Притоки и оттоки.

Чистый приток за месяц по фонду:

    приток = СЧА_конец − СЧА_начало × (Пай_конец / Пай_начало)

Второе слагаемое показывает, какой была бы СЧА, если бы деньги не приходили и не уходили.
Всё, что сверх него, принесли пайщики. Пай берётся сопоставимый, то есть после дроблений.

Считаем в валюте пая, в рубли переводим по курсу ЦБ на конец месяца: переоценка валюты притоком
не считается. Если у месяца нет начальной или конечной точки, приток не считается и остаётся
пропуском. Суммы только складываются, поэтому итоги обязаны сходиться копейка в копейку.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from pif.models import Fund
from pif.normalize.fx import FxRates
from pif.normalize.series import Correction, NormalizedSeries, Point, month_end_points, month_key

CENT = Decimal("0.01")
MAX_FLOW_TO_NAV = 5


@dataclass(frozen=True, slots=True)
class MonthlyFlow:
    rule_number: str
    month: str
    start_day: date | None
    end_day: date
    nav_start: Decimal | None
    nav_end: Decimal
    price_start: Decimal | None
    price_end: Decimal
    flow: Decimal
    market_effect: Decimal
    flow_rub: Decimal | None
    kind: str  # regular | initial


def _previous_month(month: str) -> str:
    year, number = (int(part) for part in month.split("-"))
    return f"{year - 1:04d}-12" if number == 1 else f"{year:04d}-{number - 1:02d}"


def net_flow(nav_start: Decimal, nav_end: Decimal, price_start: Decimal, price_end: Decimal) -> Decimal:
    return nav_end - nav_start * (price_end / price_start)


def fund_flows(fund: Fund, series: NormalizedSeries, fx: FxRates, last_complete: date) -> list[MonthlyFlow]:
    ends = month_end_points(series.points)
    limit = month_key(last_complete)
    flows: list[MonthlyFlow] = []
    for month in sorted(ends):
        if month > limit:
            continue
        end = ends[month]
        start = ends.get(_previous_month(month))
        rate = fx.rate(end.currency, end.day)
        if start is None:
            flow = _initial_flow(fund, month, end)
            if flow is None:
                continue
            value, kind, effect = flow, "initial", Decimal(0)
        else:
            value = net_flow(start.nav, end.nav, start.adj_price, end.adj_price)
            if abs(value) > MAX_FLOW_TO_NAV * max(start.nav, end.nav):
                # Такого притока не бывает: значит, ошибочна одна из точек. Лучше пропуск, чем ложная цифра.
                series.corrections.append(
                    Correction(
                        fund.rule_number,
                        end.day,
                        "flow",
                        str(value.quantize(CENT)),
                        "",
                        "Неправдоподобный приток: точка на начало или конец месяца ошибочна, месяц пропущен",
                        end.source_id,
                    )
                )
                continue
            effect = end.nav - start.nav - value
            kind = "regular"
        flows.append(
            MonthlyFlow(
                rule_number=fund.rule_number,
                month=month,
                start_day=start.day if start else None,
                end_day=end.day,
                nav_start=start.nav if start else None,
                nav_end=end.nav,
                price_start=start.adj_price if start else None,
                price_end=end.adj_price,
                flow=value.quantize(CENT),
                market_effect=effect.quantize(CENT),
                flow_rub=(value * rate).quantize(CENT) if rate is not None else None,
                kind=kind,
            )
        )
    return flows


def _initial_flow(fund: Fund, month: str, end: Point) -> Decimal | None:
    """Месяц завершения формирования: всё СЧА внесли пайщики, начальной точки нет и быть не могло."""
    if fund.formed_on and month_key(fund.formed_on) == month:
        return end.nav
    return None


@dataclass(slots=True)
class FlowTotals:
    """Итоги по разрезам. market считается отдельно от разрезов, а не из них."""

    by_fund: dict[str, dict[str, Decimal]] = field(default_factory=lambda: defaultdict(dict))
    by_uk: dict[str, dict[str, Decimal]] = field(default_factory=lambda: defaultdict(dict))
    by_type: dict[str, dict[str, Decimal]] = field(default_factory=lambda: defaultdict(dict))
    by_category: dict[str, dict[str, Decimal]] = field(default_factory=lambda: defaultdict(dict))
    by_asset_class: dict[str, dict[str, Decimal]] = field(default_factory=lambda: defaultdict(dict))
    market: dict[str, Decimal] = field(default_factory=dict)


def _add(table: dict[str, dict[str, Decimal]], key: str, month: str, value: Decimal) -> None:
    table[key][month] = table[key].get(month, Decimal(0)) + value


def aggregate(funds: Iterable[Fund], flows: Iterable[MonthlyFlow]) -> FlowTotals:
    registry = {f.rule_number: f for f in funds}
    totals = FlowTotals()
    for flow in flows:
        if flow.flow_rub is None:
            continue
        fund = registry[flow.rule_number]
        month, value = flow.month, flow.flow_rub
        _add(totals.by_fund, fund.rule_number, month, value)
        _add(totals.by_uk, fund.uk_inn, month, value)
        _add(totals.by_type, fund.fund_type.value, month, value)
        _add(totals.by_category, fund.cbr_category, month, value)
        _add(totals.by_asset_class, fund.asset_class, month, value)
        totals.market[month] = totals.market.get(month, Decimal(0)) + value
    return totals


@dataclass(frozen=True, slots=True)
class TieOut:
    level: str
    month: str
    expected: Decimal
    actual: Decimal

    @property
    def difference(self) -> Decimal:
        return self.actual - self.expected


def check_tie_out(totals: FlowTotals) -> list[TieOut]:
    """Сверка: сумма по каждому разрезу равна итогу рынка за каждый месяц."""
    levels: dict[str, dict[str, dict[str, Decimal]]] = {
        "фонды": totals.by_fund,
        "УК": totals.by_uk,
        "типы": totals.by_type,
        "категории": totals.by_category,
        "классы активов": totals.by_asset_class,
    }
    broken: list[TieOut] = []
    for level, table in levels.items():
        for month, expected in totals.market.items():
            actual = sum((row.get(month, Decimal(0)) for row in table.values()), Decimal(0))
            if actual != expected:
                broken.append(TieOut(level, month, expected, actual))
    return broken


def rolling_sum(monthly: dict[str, Decimal], months: list[str]) -> Decimal | None:
    """Сумма за период. Если хотя бы одного месяца нет, сумма была бы занижена, поэтому None."""
    if not months or any(m not in monthly for m in months):
        return None
    return sum((monthly[m] for m in months), Decimal(0))


def last_months(end: str, count: int) -> list[str]:
    result = [end]
    while len(result) < count:
        result.append(_previous_month(result[-1]))
    return list(reversed(result))


AggregateKey = Callable[[Fund], str]
