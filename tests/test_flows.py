"""Притоки на примере с заранее известными числами и сверка итогов."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

from conftest import make_fund, observation
from pif.flows.compute import aggregate, check_tie_out, fund_flows, last_months, net_flow, rolling_sum
from pif.models import FundType
from pif.normalize.fx import FxRates
from pif.normalize.series import normalize

FX = FxRates()
LAST = date(2026, 3, 31)


def flows_for(fund, points, fx=FX):
    data = {day: observation(day, nav, price, currency) for day, nav, price, currency in points}
    return fund_flows(fund, normalize(fund.rule_number, data, fx), fx, LAST)


def test_formula_on_known_numbers():
    # СЧА 1000 при пае 10. Пай вырос на 5 %, без притоков было бы 1050. Стало 1200, значит пришло 150.
    assert net_flow(D(1000), D(1200), D(10), D("10.5")) == D(150)


def test_pure_market_growth_gives_zero_flow():
    assert net_flow(D(1000), D(1100), D(10), D(11)) == D(0)


def test_outflow_is_negative():
    assert net_flow(D(1000), D(900), D(10), D(11)) == D(-200)


def test_monthly_flow_and_market_effect():
    fund = make_fund()
    points = [
        (date(2026, 1, 30), "1000", "10", "RUB"),
        (date(2026, 2, 27), "1200", "10.5", "RUB"),
    ]
    (flow,) = flows_for(fund, points)
    assert flow.month == "2026-02"
    assert flow.flow == D("150.00")
    assert flow.market_effect == D("50.00")
    assert flow.flow + flow.market_effect == D(1200) - D(1000)


def test_last_published_point_of_month_is_used():
    fund = make_fund()
    points = [
        (date(2026, 1, 15), "500", "9", "RUB"),
        (date(2026, 1, 30), "1000", "10", "RUB"),
        (date(2026, 2, 10), "700", "12", "RUB"),
        (date(2026, 2, 27), "1200", "10.5", "RUB"),
    ]
    (flow,) = flows_for(fund, points)
    assert flow.start_day == date(2026, 1, 30)
    assert flow.end_day == date(2026, 2, 27)


def test_missing_month_stays_a_gap():
    fund = make_fund()
    points = [
        (date(2026, 1, 30), "1000", "10", "RUB"),
        (date(2026, 3, 31), "1200", "10.5", "RUB"),
    ]
    assert flows_for(fund, points) == []


def test_formation_month_counts_all_nav_as_inflow():
    fund = make_fund(formed_on=date(2026, 2, 10))
    (flow,) = flows_for(fund, [(date(2026, 2, 27), "5000", "100", "RUB")])
    assert flow.kind == "initial"
    assert flow.flow == D("5000.00")


def test_unfinished_month_is_not_counted():
    fund = make_fund()
    points = [
        (date(2026, 3, 31), "1000", "10", "RUB"),
        (date(2026, 4, 15), "1200", "10", "RUB"),
    ]
    assert flows_for(fund, points) == []


def test_split_does_not_create_a_fake_flow():
    fund = make_fund()
    points = [
        (date(2026, 1, 30), "1000000", "1000", "RUB"),
        (date(2026, 2, 27), "1050000", "105", "RUB"),
    ]
    (flow,) = flows_for(fund, points)
    assert abs(flow.flow) < D("1")


def test_currency_fund_flow_excludes_fx_revaluation():
    fund = make_fund()
    rates = FxRates({"USD": {date(2026, 1, 30): D("80"), date(2026, 2, 27): D("100")}})
    points = [
        (date(2026, 1, 30), "1000", "10", "USD"),
        (date(2026, 2, 27), "1000", "10", "USD"),
    ]
    (flow,) = flows_for(fund, points, rates)
    assert flow.flow_rub == D("0.00"), "рост доллара притоком не считается"


def test_totals_tie_out_across_all_levels():
    funds = [
        make_fund("1", uk_inn="111", fund_type=FundType.OPEN, asset_class="Акции"),
        make_fund("2", uk_inn="111", fund_type=FundType.EXCHANGE, asset_class="Облигации"),
        make_fund("3", uk_inn="222", fund_type=FundType.OPEN, asset_class="Акции", cbr_category="Комбинированный фонд"),
    ]
    flows = []
    for fund, values in zip(funds, [("1000", "1100"), ("500", "450"), ("300", "390")], strict=True):
        data = [
            (date(2026, 1, 30), values[0], "10", "RUB"),
            (date(2026, 2, 27), values[1], "10", "RUB"),
        ]
        flows += flows_for(fund, data)
    totals = aggregate(funds, flows)
    assert totals.market["2026-02"] == D("140.00")
    assert totals.by_uk["111"]["2026-02"] == D("50.00")
    assert totals.by_uk["222"]["2026-02"] == D("90.00")
    assert totals.by_type["Открытый"]["2026-02"] == D("190.00")
    assert totals.by_type["Биржевой"]["2026-02"] == D("-50.00")
    assert check_tie_out(totals) == []


def test_tie_out_detects_a_broken_total():
    fund = make_fund()
    flows = flows_for(fund, [(date(2026, 1, 30), "1000", "10", "RUB"), (date(2026, 2, 27), "1100", "10", "RUB")])
    totals = aggregate([fund], flows)
    totals.by_uk[fund.uk_inn]["2026-02"] += D("0.01")
    broken = check_tie_out(totals)
    assert [b.level for b in broken] == ["УК"]
    assert broken[0].difference == D("0.01")


def test_period_sum_is_empty_if_a_month_is_missing():
    monthly = {"2026-01": D(1), "2026-03": D(2)}
    assert rolling_sum(monthly, ["2026-01", "2026-02", "2026-03"]) is None
    assert rolling_sum(monthly, ["2026-03"]) == D(2)


def test_last_months_crosses_year_boundary():
    assert last_months("2026-02", 3) == ["2025-12", "2026-01", "2026-02"]


def test_impossible_flow_is_dropped_and_logged():
    fund = make_fund()
    data = {
        date(2026, 1, 30): observation(date(2026, 1, 30), "1000", "1"),
        date(2026, 2, 27): observation(date(2026, 2, 27), "5000", "100000"),
    }
    series = normalize(fund.rule_number, data, FX)
    assert fund_flows(fund, series, FX, LAST) == []
    assert any("Неправдоподобный" in c.reason for c in series.corrections)
