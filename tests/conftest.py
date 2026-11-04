from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from pif.models import Fund, FundType, Observation

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_bytes():
    def read(name: str) -> bytes:
        return (FIXTURES / name).read_bytes()

    return read


@pytest.fixture
def fixture_text(fixture_bytes):
    def read(name: str, encoding: str = "utf-8") -> str:
        return fixture_bytes(name).decode(encoding)

    return read


def make_fund(rule: str = "0001-00000001", **overrides) -> Fund:
    values = {
        "rule_number": rule,
        "name": "Открытый паевой инвестиционный фонд рыночных финансовых инструментов «Тестовый»",
        "short_name": "ОПИФ рыночных финансовых инструментов «Тестовый»",
        "fund_type": FundType.OPEN,
        "cbr_category": "Фонд рыночных финансовых инструментов",
        "asset_class": "Облигации",
        "uk_inn": "7700000000",
        "uk_ogrn": "1027700000000",
        "uk_name": "ООО УК Тест",
        "registered_on": date(2010, 1, 1),
        "formed_on": date(2010, 3, 1),
        "cbr_status": "Сформирован",
        "for_qualified": False,
        "exchange_traded": False,
    }
    values.update(overrides)
    return Fund(**values)


def observation(day: date, nav: str | None, price: str | None, currency: str = "RUB", kind: str = "daily") -> Observation:
    return Observation(
        day,
        Decimal(nav) if nav is not None else None,
        Decimal(price) if price is not None else None,
        currency,
        kind,
        "src",
    )
