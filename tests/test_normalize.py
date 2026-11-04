from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

from conftest import observation

from pif.normalize.fx import FxRates
from pif.normalize.series import month_end_points, normalize

FX = FxRates()


def series_of(values):
    start = date(2026, 1, 5)
    points = {}
    for index, (nav, price) in enumerate(values):
        day = start + timedelta(days=index)
        points[day] = observation(day, nav, price)
    return points


def test_split_is_adjusted_and_logged():
    # Дробление 1 к 10: пай упал в 10 раз, СЧА на месте.
    data = series_of([("1000000", "1000"), ("1010000", "1010"), ("1020000", "102"), ("1030000", "103")])
    result = normalize("r", data, FX)
    prices = [p.adj_price for p in result.points]
    assert float(prices[0]) == 100.0
    assert float(prices[1]) == 101.0
    assert prices[2] == D("102")
    assert [p.unit_price for p in result.points][0] == D("1000")
    assert len(result.corrections) == 1
    assert "Дробление" in result.corrections[0].reason


def test_real_price_crash_with_nav_crash_is_not_a_split():
    data = series_of([("1000", "100"), ("1000", "100"), ("500", "50"), ("500", "50")])
    result = normalize("r", data, FX)
    assert [p.adj_price for p in result.points] == [D("100"), D("100"), D("50"), D("50")]
    assert result.corrections == []


def test_big_redemption_does_not_touch_price():
    data = series_of([("1000", "100"), ("100", "100"), ("100", "101")])
    result = normalize("r", data, FX)
    assert result.corrections == []
    assert result.points[0].nav == D("1000")


def test_one_day_spike_is_excluded_and_logged():
    data = series_of([("1000", "100"), ("1000", "101"), ("1000", "1010"), ("1000", "102"), ("1000", "103")])
    result = normalize("r", data, FX)
    spike = result.points[2]
    assert spike.flag == "spike"
    assert spike.adj_price is None
    assert spike.unit_price == D("1010"), "опубликованное значение не должно исчезать"
    assert any("выброс" in c.reason for c in result.corrections)


def test_nav_in_thousands_is_rescaled_and_logged():
    data = series_of([("1000000", "100"), ("1010000", "101"), ("1020", "102"), ("1030000", "103"), ("1040000", "104")])
    result = normalize("r", data, FX)
    assert result.points[2].nav == D("1020000")
    assert "единицах" in result.corrections[0].reason


def test_gap_is_visible_and_not_filled():
    first, second = date(2026, 1, 5), date(2026, 3, 20)
    points = {first + timedelta(days=i): observation(first + timedelta(days=i), "1000", "100") for i in range(5)}
    points |= {second + timedelta(days=i): observation(second + timedelta(days=i), "1000", "100") for i in range(5)}
    result = normalize("r", points, FX)
    assert len(result.points) == 10
    assert result.frequency == "daily"
    assert len(result.gaps) == 1
    assert result.gaps[0].start == first + timedelta(days=4)
    assert result.gaps[0].end == second


def test_monthly_series_has_no_false_gaps():
    days = [date(2026, 1, 30), date(2026, 2, 27), date(2026, 3, 31), date(2026, 4, 30)]
    result = normalize("r", {d: observation(d, "1000", "100", kind="monthly_doc") for d in days}, FX)
    assert result.frequency == "monthly"
    assert result.gaps == []


def test_non_positive_values_are_dropped_and_logged():
    data = series_of([("1000", "100"), ("0", "101"), ("1000", "-5")])
    result = normalize("r", data, FX)
    assert result.points[1].nav is None
    assert result.points[2].unit_price is None
    assert len(result.corrections) == 2


def test_values_are_converted_to_rubles():
    day = date(2026, 1, 12)
    rates = FxRates({"USD": {day: D("90")}})
    result = normalize("r", {day: observation(day, "100", "10", currency="USD")}, rates)
    point = result.points[0]
    assert point.nav == D("100")
    assert point.nav_rub == D("9000")
    assert point.price_rub == D("900")
    assert result.currency == "USD"


def test_missing_rate_leaves_ruble_value_empty():
    day = date(2026, 1, 12)
    result = normalize("r", {day: observation(day, "100", "10", currency="USD")}, FX)
    assert result.points[0].nav_rub is None


def test_month_end_takes_last_published_point_with_both_values():
    days = {
        date(2026, 1, 29): observation(date(2026, 1, 29), "1000", "100"),
        date(2026, 1, 30): observation(date(2026, 1, 30), "1100", "110"),
        date(2026, 1, 31): observation(date(2026, 1, 31), None, "111"),
    }
    result = normalize("r", days, FX)
    assert month_end_points(result.points)["2026-01"].day == date(2026, 1, 30)
