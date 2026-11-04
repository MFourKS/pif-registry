"""Доменная модель. Деньги и стоимость пая хранятся в Decimal: float исказил бы сверку сумм."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum


class FundType(StrEnum):
    OPEN = "Открытый"
    INTERVAL = "Интервальный"
    EXCHANGE = "Биржевой"


class DataStatus(StrEnum):
    """Статус данных фонда. Подпись показывается на сайте и в отчёте качества."""

    OK = "ok"
    PARTIAL = "partial"
    FORMED_LATER = "formed_later"
    NOT_FORMED = "not_formed"
    NO_HISTORY = "no_history"
    SITE_UNAVAILABLE = "site_unavailable"
    BLOCKED = "blocked"
    NOT_FOUND = "not_found"
    PARSE_ERROR = "parse_error"


STATUS_LABELS: dict[DataStatus, str] = {
    DataStatus.OK: "Ряд собран",
    DataStatus.PARTIAL: "Ряд неполный",
    DataStatus.FORMED_LATER: "Фонд сформирован позже",
    DataStatus.NOT_FORMED: "Фонд ещё формируется",
    DataStatus.NO_HISTORY: "УК не публикует историю",
    DataStatus.SITE_UNAVAILABLE: "Сайт УК недоступен",
    DataStatus.BLOCKED: "Сайт УК блокирует автоматический доступ",
    DataStatus.NOT_FOUND: "Данные фонда не найдены на сайте УК",
    DataStatus.PARSE_ERROR: "Ошибка разбора источника",
}


@dataclass(frozen=True, slots=True)
class ManagementCompany:
    inn: str
    ogrn: str
    name: str
    short_name: str
    site: str | None
    sravnenie_id: str | None = None


@dataclass(slots=True)
class Fund:
    rule_number: str
    name: str
    short_name: str
    fund_type: FundType
    cbr_category: str
    asset_class: str
    uk_inn: str
    uk_ogrn: str
    uk_name: str
    registered_on: date | None
    formed_on: date | None
    cbr_status: str
    for_qualified: bool
    exchange_traded: bool
    previous_names: list[str] = field(default_factory=list)
    isin: str | None = None
    ticker: str | None = None
    currency: str = "RUB"

    def to_json(self) -> dict[str, object]:
        data = asdict(self)
        data["fund_type"] = self.fund_type.value
        data["registered_on"] = self.registered_on.isoformat() if self.registered_on else None
        data["formed_on"] = self.formed_on.isoformat() if self.formed_on else None
        return data


@dataclass(frozen=True, slots=True)
class Observation:
    """Одна опубликованная точка ряда с привязкой к сохранённому источнику."""

    day: date
    nav: Decimal | None
    unit_price: Decimal | None
    currency: str
    kind: str  # daily | monthly_doc
    source_id: str


@dataclass(frozen=True, slots=True)
class OfficialReturn:
    """Официальная доходность из ключевого информационного документа."""

    rule_number: str
    report_date: date
    months: int
    value_pct: Decimal
    source_id: str


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """Строка манифеста: что скачано, откуда, когда и с каким хешем."""

    source_id: str
    url: str
    fetched_at: str
    fmt: str
    sha256: str
    size: int
    http_status: int
    status: str
    uk_inn: str = ""
    fund: str = ""
    note: str = ""
