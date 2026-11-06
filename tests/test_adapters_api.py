"""Ряды из API сайтов УК: настоящие ответы, значения сверены с КИД и страницей раскрытия."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal as D

import pytest

from pif.adapters.april import kid_links, price_rows
from pif.adapters.catalogs import ingos_rows
from pif.errors import UnknownFormatError
from pif.flows.compute import is_rounded_nav


def test_april_prices_are_in_kopecks_and_dates_in_moscow_time(fixture_bytes):
    item = json.loads(fixture_bytes("april_fund_details.json"))["success"][0]
    rows = {row.day: row for row in price_rows(item, "april")}
    # КИД на 30.09.2026: пай 1 089,93 ₽, СЧА 410 млн ₽.
    assert rows[date(2026, 9, 30)].unit_price == D("1089.93")
    assert rows[date(2026, 9, 30)].nav == D("410022910.70")


def test_april_point_without_price_is_an_explicit_error():
    with pytest.raises(UnknownFormatError):
        price_rows({"priceList": [{"date": 1790715600, "assetValue": 1}]}, "april")


def test_april_kid_links_use_public_download_address():
    listing = {"success": {"KID": {"children": [{"id": 8726, "title": "Ключевой информационный документ на 30.09.2026"}]}}}
    link = kid_links(listing, "page")[0]
    assert link.url.endswith("v01_download?code=SITE&id=8726")
    assert link.text.endswith("30.09.2026")


def test_ingos_quotes(fixture_bytes):
    rows = ingos_rows(json.loads(fixture_bytes("ingos_quotes.json")), "ingos")
    assert rows[-1].day == date(2026, 11, 3)
    assert rows[-1].unit_price == D("22444.25")
    assert rows[-1].nav == D("99277167.24")


def test_ingos_unknown_point_is_an_explicit_error():
    with pytest.raises(UnknownFormatError):
        ingos_rows([{"date": "2026-11-03T00:00:00"}], "ingos")


@pytest.mark.parametrize(
    ("nav", "expected"),
    [(D("977610000.00"), True), (D("977610000.01"), False), (D("99277167.24"), False), (None, False), (D(0), False)],
)
def test_rounded_nav_is_detected(nav, expected):
    assert is_rounded_nav(nav) is expected
