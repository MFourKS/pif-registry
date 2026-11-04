from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from conftest import make_fund, observation
from pif.matching import FundMatcher, name_tokens, rule_numbers_in
from pif.models import FundType, OfficialReturn
from pif.normalize.fx import FxRates
from pif.normalize.series import normalize
from pif.verify.yields import check_fund, computed_return, shift_months


def series_with(points):
    return normalize("r", {d: observation(d, "1000", price) for d, price in points}, FxRates())


@pytest.mark.parametrize(
    ("day", "months", "expected"),
    [
        (date(2026, 9, 30), 1, date(2026, 8, 31)),
        (date(2026, 9, 30), 12, date(2025, 9, 30)),
        (date(2026, 3, 31), 1, date(2026, 2, 28)),
        (date(2026, 1, 15), 3, date(2025, 10, 15)),
        (date(2026, 3, 30), 1, date(2026, 2, 28)),
        (date(2026, 10, 30), 3, date(2026, 7, 31)),
    ],
)
def test_shift_months(day, months, expected):
    assert shift_months(day, months) == expected


def test_computed_return_on_known_numbers():
    series = series_with([(date(2026, 8, 31), "100"), (date(2026, 9, 30), "110")])
    value, start, end = computed_return(series.points, date(2026, 9, 30), 1)
    assert value == D("10")
    assert start.day == date(2026, 8, 31)
    assert end.day == date(2026, 9, 30)


def test_no_look_ahead_when_start_point_is_missing():
    series = series_with([(date(2026, 9, 1), "100"), (date(2026, 9, 30), "110")])
    value, start, _ = computed_return(series.points, date(2026, 9, 30), 1)
    assert value is None and start is None


def test_stale_end_point_is_not_used():
    series = series_with([(date(2026, 8, 31), "100"), (date(2026, 9, 10), "110")])
    assert computed_return(series.points, date(2026, 9, 30), 1)[0] is None


def test_check_fund_uses_latest_report_and_flags_difference():
    series = series_with([(date(2026, 7, 31), "90"), (date(2026, 8, 31), "100"), (date(2026, 9, 30), "110")])
    official = [
        OfficialReturn("r", date(2026, 8, 31), 1, D("11.11"), "old"),
        OfficialReturn("r", date(2026, 9, 30), 1, D("9.0"), "new"),
    ]
    (check,) = check_fund(series, official)
    assert check.report_date == date(2026, 9, 30)
    assert check.computed_pct == D("10")
    assert check.difference_pp == D("1.0")
    assert check.source_id == "new"


FUNDS = [
    make_fund("1", name="ОПИФ рыночных финансовых инструментов «Альфа-Капитал Облигации Плюс»", short_name="a"),
    make_fund("2", name="ОПИФ рыночных финансовых инструментов «Альфа-Капитал Акции компаний роста»", short_name="b"),
    make_fund(
        "3",
        name="Биржевой паевой инвестиционный фонд рыночных финансовых инструментов «Альфа-Капитал Облигации Плюс»",
        short_name="c",
        fund_type=FundType.EXCHANGE,
    ),
    make_fund("4", name="ОПИФ рыночных финансовых инструментов «Ликвидность»", short_name="d", previous_names=["ОПИФ «Старое имя фонда»"]),
]


def test_match_exact_quoted_name():
    matcher = FundMatcher(FUNDS)
    fund = matcher.match("Открытый паевой инвестиционный фонд рыночных финансовых инструментов «Альфа-Капитал Облигации Плюс»")
    assert fund.rule_number == "1"


def test_fund_type_separates_namesakes():
    matcher = FundMatcher(FUNDS)
    fund = matcher.match("Биржевой паевой инвестиционный фонд «Альфа-Капитал Облигации Плюс»")
    assert fund.rule_number == "3"


def test_ambiguous_name_gives_no_match():
    matcher = FundMatcher(FUNDS)
    assert matcher.match("«Альфа-Капитал Облигации Плюс»") is None


def test_previous_name_is_recognised():
    assert FundMatcher(FUNDS).match("ОПИФ «Старое имя фонда»").rule_number == "4"


def test_unrelated_name_gives_no_match():
    assert FundMatcher(FUNDS).match("ОПИФ «Совсем другой фонд»") is None


def test_brand_words_do_not_decide_the_match():
    matcher = FundMatcher(FUNDS, brand_words=["альфа", "капитал"])
    assert matcher.match("ОПИФ Облигации Плюс").rule_number == "1"


def test_rule_numbers_in_text():
    text = "Правила зарегистрированы 13.09.2007 № 0968-94131582, также 0241–74 050 443 и № 5313-СД"
    assert rule_numbers_in(text) == {"0968-94131582", "0241-74050443", "5313-СД"}


def test_name_tokens_ignore_boilerplate():
    assert name_tokens("Открытый паевой инвестиционный фонд рыночных финансовых инструментов Тестовый") == {"тестовый"}


def test_company_after_fund_name_is_ignored():
    tokens = name_tokens("Открытый паевой инвестиционный фонд ДОХОДЪ. Биржевая копилка Под управлением: ООО «УК «ДОХОДЪ»")
    assert tokens == {"доходъ", "биржевая", "копилка"}


def test_closed_fund_is_never_matched_to_open_namesake():
    matcher = FundMatcher([make_fund("2565", name="ОПИФ рыночных финансовых инструментов «Чистые деньги»")])
    assert matcher.match("ЗПИФ рыночных финансовых инструментов «Заблокированные активы фонда «Чистые деньги»") is None
