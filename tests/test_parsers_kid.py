"""КИД: настоящие файлы разных УК и вёрсток. Ожидаемые значения сняты глазами с самих документов."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from pif.errors import UnknownFormatError
from pif.parsers.kid import parse_kid, parse_kid_text
from pif.parsers.nav_statement import parse_nav_statement

CASES = [
    # файл, дата, пай, СЧА, доходность
    ("kid_alfa.docx", date(2026, 9, 30), "7203.41", "44343047190.21", {1: "-1.5", 3: "0.6", 6: "-0.1", 12: "8.5"}),
    ("kid_finam.docx", date(2026, 9, 30), "252.26", "29250345.74", {1: "3.20", 3: "-2.77", 6: "-16.88", 12: "-14.56"}),
    ("kid_akbars_two_columns.pdf", date(2026, 9, 30), "1064.25", "46085771.04", {1: "3.38", 3: "6.87", 6: "-12.70", 12: "-11.07"}),
    ("kid_tkb.pdf", date(2026, 9, 30), "4965.94", "659866237.72", {1: "-8.15", 3: "6.59", 6: "-9.98", 12: "4.83"}),
    ("kid_trinfico.pdf", date(2026, 9, 30), "684.91", "233076124.30", {1: "-1.33", 3: "0.52", 6: "-2.85", 12: "2.53"}),
    ("kid_rshb_young_fund.pdf", date(2026, 9, 30), "1025.19", "593841217.27", {1: "0.68", 3: "2.64"}),
    ("kid_record_millions.pdf", date(2026, 9, 30), "9912.89", "26690000", {1: "0.54", 3: "1.24"}),
    ("kid_mkb_horizontal.pdf", date(2026, 8, 31), "368.63", "8000000", {1: "0", 3: "-0.08", 6: "-0.25", 12: "-0.46"}),
]


@pytest.mark.parametrize(("name", "day", "price", "nav", "returns"), CASES)
def test_real_kid_documents(fixture_bytes, name, day, price, nav, returns):
    fmt = name.rsplit(".", 1)[1]
    kid = parse_kid(fixture_bytes(name), fmt, name)
    assert kid.report_date == day
    assert kid.unit_price == D(price)
    assert kid.nav == D(nav)
    assert kid.returns == {months: D(value) for months, value in returns.items()}


def test_blank_periods_of_young_fund_are_not_filled(fixture_bytes):
    kid = parse_kid(fixture_bytes("kid_rshb_young_fund.pdf"), "pdf")
    assert 6 not in kid.returns and 12 not in kid.returns


def test_fund_name_is_found_inside_document(fixture_bytes):
    kid = parse_kid(fixture_bytes("kid_alfa.docx"), "docx")
    assert "Альфа-Капитал Облигации Плюс" in kid.fund_name


def test_not_a_kid_gives_clear_error():
    with pytest.raises(UnknownFormatError, match="не похож на КИД"):
        parse_kid_text("Отчёт по состоянию на 30.09.2026. Просто текст.")


def test_document_without_date_gives_clear_error():
    with pytest.raises(UnknownFormatError, match="нет даты"):
        parse_kid_text("Ключевой информационный документ без даты")


def test_corrupted_docx_is_an_error_not_a_zero(fixture_bytes):
    broken = fixture_bytes("kid_alfa.docx")[:2000]
    with pytest.raises(UnknownFormatError):
        parse_kid(broken, "docx")


def test_unsupported_document_format():
    with pytest.raises(UnknownFormatError, match="не поддержан"):
        parse_kid(b"<html></html>", "html")


def test_list_of_all_funds_in_header_does_not_confuse_name():
    text = """Открытый паевой инвестиционный фонд рыночных финансовых инструментов «Чужой фонд» – правила № 1.
    Открытый паевой инвестиционный фонд рыночных финансовых инструментов «Ещё один» – правила № 2.
    КЛЮЧЕВОЙ ИНФОРМАЦИОННЫЙ ДОКУМЕНТ
    Раздел 1. Общие сведения
    Ключевой информационный документ по состоянию на 30.09.2026
    Открытый паевой инвестиционный фонд рыночных финансовых инструментов «Свой фонд» под управлением ООО УК
    Раздел 2. Внимание"""
    assert "Свой фонд" in parse_kid_text(text).fund_name


def test_name_without_quotes_stops_before_company():
    text = """Ключевой информационный документ по состоянию на 30.09.2026
    Открытый паевой инвестиционный фонд рыночных финансовых инструментов ДОХОДЪ. Облигации - Храброе Сердце
    Под управлением: ООО «УК «ДОХОДЪ»"""
    name = parse_kid_text(text).fund_name
    assert "Храброе Сердце" in name and "УК" not in name


def test_heading_name_wins_over_list_of_funds():
    text = """Открытый паевой инвестиционный фонд рыночных финансовых инструментов «Арикапитал – Чистые деньги» – правила 2565.
    Открытый паевой инвестиционный фонд рыночных финансовых инструментов «Арикапитал – Глобальные инвестиции» – правила 3208.
    Ключевой информационный документ ОПИФ рыночных финансовых инструментов «Арикапитал - Глобальные инвестиции»
    Опубликовано: 07.08.2025, 15:30
    Раздел 1. Общие сведения
    Ключевой информационный документ по состоянию на 31.07.2025"""
    assert "Глобальные инвестиции" in parse_kid_text(text).fund_name


def test_nav_statement_form_0420502(fixture_bytes):
    statement = parse_nav_statement(fixture_bytes("nav_statement_tcapital.pdf"), "pdf")
    assert statement.report_date == date(2026, 9, 30)
    assert statement.nav == D("17767367596.16")
    assert statement.unit_price == D("9.8259")
    assert statement.isin == "RU000A1011U5"


def test_kid_is_not_taken_for_nav_statement(fixture_bytes):
    with pytest.raises(UnknownFormatError):
        parse_nav_statement(fixture_bytes("kid_tkb.pdf"), "pdf")
