from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from pif.errors import UnknownFormatError
from pif.models import FundType
from pif.normalize.fx import FxRates, parse_dynamic
from pif.registry import cbr, moex
from pif.registry.build import parse_sravnenie_ids
from pif.registry.classify import asset_class


def test_cbr_registry_keeps_only_active_retail_types(fixture_bytes):
    as_of, funds = cbr.parse_registry(fixture_bytes("cbr_list_pif_sample.xlsx"))
    assert as_of == date(2026, 11, 4)
    assert {f.fund_type for f in funds} == {FundType.OPEN, FundType.INTERVAL, FundType.EXCHANGE}
    assert all(f.status != "Исключён из реестра" for f in funds)
    assert len(funds) == 9
    assert all(f.rule_number and f.uk_ogrn for f in funds)


def test_cbr_registry_with_renamed_column_fails_loudly(fixture_bytes, tmp_path):
    import io

    import openpyxl

    book = openpyxl.load_workbook(io.BytesIO(fixture_bytes("cbr_list_pif_sample.xlsx")))
    sheet = book.active
    for cell in sheet[3]:
        if cell.value == "Статус":
            cell.value = "Состояние"
    buffer = io.BytesIO()
    book.save(buffer)
    with pytest.raises(UnknownFormatError, match="Статус"):
        cbr.parse_registry(buffer.getvalue())


def test_cbr_card_gives_inn_and_site(fixture_text):
    card = cbr.parse_card("1027739292283", fixture_text("cbr_card_alfa.html"))
    assert card.inn == "7728142469"
    assert card.site == "www.alfacapital.ru"
    assert "Альфа-Капитал" in card.short_name


def test_cbr_card_without_inn_is_an_error():
    with pytest.raises(UnknownFormatError):
        cbr.parse_card("1", "<html>нет данных</html>")


def test_moex_page(fixture_bytes):
    securities = moex.parse_page(fixture_bytes("moex_securities.json"))
    assert securities
    assert all(s.isin and s.regnumber for s in securities)


def test_moex_lookup_by_rule_prefix():
    security = moex.MoexSecurity("SBMX", "RU000A0ZZH92", "3555", "", "7710183778", "exchange_ppif", True)
    index = moex.index_by_rule([security])
    assert moex.lookup(index, "3555") is security
    assert moex.lookup(index, "3555-СД") is security
    assert moex.lookup(index, "9999") is None


def test_sravnenie_ids():
    page = '<tr><td><a href="/uk/17" title="X">X</a></td><td class="muted mono">7710183778</td></tr>'
    assert parse_sravnenie_ids(page) == {"7710183778": "17"}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ОПИФ рыночных финансовых инструментов «Облигации Плюс»", "Облигации"),
        ("БПИФ рыночных финансовых инструментов «Денежный рынок»", "Денежный рынок"),
        ("ОПИФ рыночных финансовых инструментов «Золото»", "Драгоценные металлы"),
        ("ОПИФ рыночных финансовых инструментов «Сбалансированный»", "Смешанные"),
        ("ИПИФ комбинированный «Хедж-фонд Д5»", "Хедж-стратегии"),
        ("ОПИФ рыночных финансовых инструментов «Левитан»", "Прочие"),
    ],
)
def test_asset_class(name, expected):
    assert asset_class(name) == expected


def test_cbr_fx_xml(fixture_bytes):
    table = parse_dynamic(fixture_bytes("cbr_fx_cny.xml").decode("cp1251"), "CNY")
    assert len(table) >= 5
    assert all(rate > 0 for rate in table.values())


def test_fx_uses_previous_rate_on_weekend_but_not_forever():
    rates = FxRates({"USD": {date(2026, 1, 9): D("90")}})
    assert rates.rate("USD", date(2026, 1, 11)) == D("90")
    assert rates.rate("USD", date(2026, 3, 1)) is None
    assert rates.rate("RUB", date(2026, 3, 1)) == D(1)
