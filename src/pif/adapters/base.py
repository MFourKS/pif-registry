"""Контракт адаптера УК и общий контекст сбора."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pif.errors import (
    AccessBlocked,
    FetchError,
    ParseError,
    ResourceNotFound,
    RobotsDisallowed,
    SiteUnavailable,
)
from pif.matching import FundMatcher
from pif.models import DataStatus, Fund, ManagementCompany, Observation, OfficialReturn
from pif.net.client import Fetcher, FetchResult
from pif.observability import get_logger
from pif.parsers.kid import KidData
from pif.parsers.series import SeriesRow
from pif.storage.series_store import DocPoint, OfficialStore, SeriesStore

log = get_logger("collect")

KID_CONFLICT_TOLERANCE = 0.02


@dataclass(slots=True)
class Issue:
    """Запись для отчёта качества."""

    severity: str  # error | warning
    stage: str  # fetch | parse | match
    uk_inn: str
    url: str
    message: str
    fund: str = ""


@dataclass(slots=True)
class FundOutcome:
    daily_points: int = 0
    doc_points: int = 0
    pages: set[str] = field(default_factory=set)
    failure: DataStatus | None = None
    failure_reason: str = ""


@dataclass(slots=True)
class UkConfig:
    """Настройки источника из config/sources.yaml."""

    adapter: str = "generic"
    seeds: list[str] = field(default_factory=list)
    follow: str | None = None
    skip: str | None = None
    max_pages: int = 150
    brand_words: list[str] = field(default_factory=list)
    fund_map: dict[str, str] = field(default_factory=dict)
    note: str = ""
    options: dict[str, Any] = field(default_factory=dict)


class CollectContext:
    def __init__(
        self,
        uk: ManagementCompany,
        funds: list[Fund],
        config: UkConfig,
        fetcher: Fetcher,
        series: SeriesStore,
        official: OfficialStore,
        cutoff: date,
    ) -> None:
        self.uk = uk
        self.funds = funds
        self.config = config
        self.fetcher = fetcher
        self.series = series
        self.official = official
        self.cutoff = cutoff
        self.matcher = FundMatcher(funds, config.brand_words)
        self.outcomes: dict[str, FundOutcome] = {f.rule_number: FundOutcome() for f in funds}
        self.issues: list[Issue] = []
        self.counters: Counter[str] = Counter()
        self.site_failure: tuple[DataStatus, str] | None = None
        # Сбой одной ссылки не значит, что сайт недоступен: это решается по итогам всего обхода.
        self.first_failure: tuple[DataStatus, str] | None = None

    async def fetch(self, url: str, *, fund: Fund | None = None, **kwargs: Any) -> FetchResult:
        return await self.fetcher.get(
            url, uk_inn=self.uk.inn, fund=fund.rule_number if fund else "", **kwargs
        )

    async def try_fetch(self, url: str, *, fund: Fund | None = None, **kwargs: Any) -> FetchResult | None:
        """Как fetch, но сбой одного адреса фиксируется и не прерывает обход."""
        try:
            return await self.fetch(url, fund=fund, **kwargs)
        except ResourceNotFound as exc:
            self.note_failure(url, exc, fund, severity="warning")
        except FetchError as exc:
            self.note_failure(url, exc, fund)
        return None

    def note_failure(self, url: str, exc: Exception, fund: Fund | None, severity: str = "warning") -> None:
        status = status_of(exc)
        self.issues.append(
            Issue(severity, "fetch", self.uk.inn, url, str(exc)[:240], fund.rule_number if fund else "")
        )
        if fund is not None:
            outcome = self.outcomes[fund.rule_number]
            outcome.failure, outcome.failure_reason = status, str(exc)[:200]
        elif self.first_failure is None:
            self.first_failure = (status, str(exc)[:200])
        self.counters[f"fetch_{status.value}"] += 1

    def note_parse_error(self, url: str, exc: ParseError, fund: Fund | None = None) -> None:
        self.issues.append(Issue("error", "parse", self.uk.inn, url, str(exc)[:300], fund.rule_number if fund else ""))
        if fund is not None:
            outcome = self.outcomes[fund.rule_number]
            outcome.failure, outcome.failure_reason = DataStatus.PARSE_ERROR, str(exc)[:200]
        self.counters["parse_errors"] += 1
        log.warning("parse_error", url=url, error=str(exc)[:200])

    def note_unmatched(self, url: str, name: str) -> None:
        self.issues.append(Issue("warning", "match", self.uk.inn, url, f"Не сопоставлено с реестром: {name[:140]}"))
        self.counters["unmatched"] += 1

    def add_series(self, fund: Fund, rows: Iterable[SeriesRow], source: FetchResult, *, kind: str = "daily") -> int:
        outcome = self.outcomes[fund.rule_number]
        added = 0
        for row in rows:
            if row.day < self.cutoff:
                continue
            observation = Observation(
                day=row.day,
                nav=row.nav,
                unit_price=row.unit_price,
                currency=row.currency or fund.currency,
                kind=kind,
                source_id=source.source_id,
            )
            added += self.series.add(fund.rule_number, observation)
            if kind == "daily":
                outcome.daily_points += 1
            else:
                outcome.doc_points += 1
        outcome.pages.add(source.url)
        self.fetcher.manifest_link(source, self.uk.inn, fund.rule_number)
        self.counters["new_points"] += added
        return added

    def add_kid(self, fund: Fund, kid: KidData, source: FetchResult) -> None:
        if kid.report_date < self.cutoff:
            return
        known = self.official.points.get((fund.rule_number, kid.report_date))
        if (
            known is not None
            and known.source_id != source.source_id
            and known.unit_price
            and kid.unit_price
            and abs(float(kid.unit_price / known.unit_price) - 1) > KID_CONFLICT_TOLERANCE
        ):
            # Два разных КИД на одну дату у одного фонда: один из них сопоставлен неверно.
            self.note_unmatched(source.url, f"КИД конфликтует с уже принятым: {kid.fund_name or ''}")
            return
        if kid.unit_price is not None or kid.nav is not None:
            row = SeriesRow(kid.report_date, kid.unit_price, kid.nav, kid.currency)
            self.add_series(fund, [row], source, kind="monthly_doc")
            self.official.add_point(
                DocPoint(
                    fund.rule_number, kid.report_date, kid.nav, kid.unit_price,
                    kid.currency or fund.currency, source.source_id,
                )
            )  # fmt: skip
        for months, value in kid.returns.items():
            self.official.add(OfficialReturn(fund.rule_number, kid.report_date, months, value, source.source_id))
        self.counters["kid_parsed"] += 1


def status_of(exc: Exception) -> DataStatus:
    if isinstance(exc, AccessBlocked | RobotsDisallowed):
        return DataStatus.BLOCKED
    if isinstance(exc, SiteUnavailable):
        return DataStatus.SITE_UNAVAILABLE
    if isinstance(exc, ResourceNotFound):
        return DataStatus.NOT_FOUND
    if isinstance(exc, ParseError):
        return DataStatus.PARSE_ERROR
    return DataStatus.SITE_UNAVAILABLE


class Adapter(ABC):
    """Адаптер знает только, где лежат данные конкретной УК. Разбор живёт в pif.parsers."""

    @abstractmethod
    async def collect(self, ctx: CollectContext) -> None: ...
