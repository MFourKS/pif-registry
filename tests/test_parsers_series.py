"""Разборщики рядов на настоящих фикстурах."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal as D

import pytest

from pif.errors import UnknownFormatError
from pif.parsers import embedded, series
from pif.parsers.numbers import parse_date, parse_decimal


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 234 567,89 ₽", "1234567.89"),
        ("7 311,93 руб.", "7311.93"),
        ("1,234,567.89", "1234567.89"),
        ("1.234.567,89", "1234567.89"),
        ("−12,5%", "-12.5"),
        (1234.5, "1234.5"),
        (10, "10"),
    ],
)
def test_parse_decimal(raw, expected):
    assert parse_decimal(raw) == D(expected)


@pytest.mark.parametrize("raw", ["", "—", "н/д", "abc", None, True, "12 мая"])
def test_parse_decimal_never_invents_zero(raw):
    assert parse_decimal(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("31.01.2025", date(2025, 1, 31)),
        ("31/01/25", date(2025, 1, 31)),
        ("2025-01-31T00:00:00Z", date(2025, 1, 31)),
        ("31 января 2025 г.", date(2025, 1, 31)),
        ("5 мая 2026", date(2026, 5, 5)),
        ("1 марта 2026", date(2026, 3, 1)),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_impossible_date_is_rejected():
    assert parse_date("31.02.2026") is None


def test_alfa_gateway_json(fixture_bytes):
    records = json.loads(fixture_bytes("alfa_gate.json"))["data"]
    rows = series.json_records(records, "alfa")
    assert len(rows) == len(records) > 10
    first = records[0]
    assert rows[0].unit_price == D(str(first["shareCost"]))
    assert rows[0].nav == D(str(first["netAssetValue"]))
    assert rows[0].currency == "RUB"


def test_first_am_chart_uses_published_price(fixture_text):
    rows = embedded.first_am_chart(fixture_text("first_chart.html"), "first")
    assert len(rows) == 45
    assert all(r.unit_price and r.nav for r in rows)
    assert rows[-1].day > rows[0].day


def test_aaa_graph_variable_has_only_unit_price(fixture_text):
    rows = embedded.js_graph_variable(fixture_text("aaa_graph.html"), "aaa")
    assert len(rows) == 40
    assert all(r.unit_price and r.nav is None for r in rows)


def test_tkb_record_array_in_escaped_attribute(fixture_text):
    rows = embedded.record_arrays(fixture_text("tkb_records.html"), "tkb")
    assert len(rows) == 40
    assert all(r.unit_price and r.nav for r in rows)
    assert all(r.nav > r.unit_price for r in rows)


def test_contrada_nested_date_map(fixture_text):
    rows = embedded.nested_date_map(fixture_text("contrada_nested.html"), "contrada")
    assert len(rows) == 40
    assert all(r.unit_price and r.nav for r in rows)


def test_wim_export_table(fixture_text):
    rows = series.html_table(fixture_text("wim_export.html"), "wim")
    assert len(rows) > 15
    by_day = {r.day: r for r in rows}
    assert by_day[date(2026, 11, 2)].unit_price == D("49.00")
    assert by_day[date(2026, 11, 2)].nav == D("10988538780.20")


def test_promsvyaz_headerless_csv(fixture_text):
    rows = series.delimited_text(fixture_text("promsvyaz_costs.csv"), "psb")
    assert rows[0].day == date(2026, 11, 2)
    assert rows[0].unit_price == D("8433.02")
    assert rows[0].nav == D("2426118491.41")


def test_extract_series_merges_all_blocks(fixture_text):
    found = embedded.extract_series(fixture_text("first_chart.html"), "first")
    assert found is not None
    name, rows = found
    assert "first_am_chart" in name
    assert rows == sorted(rows, key=lambda r: r.day)


def test_table_without_value_columns_is_an_error():
    page = "<table><tr><th>Дата</th><th>Комментарий</th></tr><tr><td>01.01.2026</td><td>тест</td></tr></table>"
    with pytest.raises(UnknownFormatError, match="нет таблицы"):
        series.html_table(page, "x")


def test_json_records_with_unknown_keys_is_an_error():
    with pytest.raises(UnknownFormatError, match="ключи"):
        series.json_records([{"foo": 1, "bar": 2}], "x")


def test_unreadable_text_table_is_an_error():
    with pytest.raises(UnknownFormatError):
        series.delimited_text("просто текст без колонок", "x")


def test_change_columns_are_not_mistaken_for_values():
    page = """<table>
    <tr><th>Дата</th><th>Изменение стоимости пая, %</th><th>Стоимость пая, руб.</th><th>СЧА, руб.</th></tr>
    <tr><td>01.10.2026</td><td>1,5</td><td>100,50</td><td>1 000 000,00</td></tr></table>"""
    rows = series.html_table(page, "x")
    assert rows[0].unit_price == D("100.50")
    assert rows[0].nav == D("1000000.00")


def test_merge_rows_joins_price_and_nav_from_different_blocks():
    day = date(2026, 1, 31)
    rows = series.merge_rows([series.SeriesRow(day, D(10), None)], [series.SeriesRow(day, None, D(1000))])
    assert rows == [series.SeriesRow(day, D(10), D(1000))]
