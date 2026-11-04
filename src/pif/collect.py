"""Этап сбора: параллельно по УК, с изоляцией сбоев.

Сайты разных УК независимы, поэтому обходим их одновременно, а лимит одного запроса в секунду
держит Fetcher на уровне хоста. Брокер здесь не нужен: очередь живёт один прогон, а задача
должна запускаться одной командой без внешних сервисов. Подробнее в docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import structlog
import yaml
from opentelemetry.trace import Status, StatusCode

from pif.adapters import ADAPTERS
from pif.adapters.base import CollectContext, FundOutcome, Issue, UkConfig, status_of
from pif.config import Settings
from pif.errors import FetchError, ParseError
from pif.models import DataStatus, ManagementCompany
from pif.net.client import Fetcher
from pif.observability import get_logger, tracer
from pif.registry.build import Registry
from pif.storage.series_store import OfficialStore, SeriesStore

log = get_logger("collect")


@dataclass(slots=True)
class CollectResult:
    outcomes: dict[str, FundOutcome] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    uk_failures: dict[str, tuple[DataStatus, str]] = field(default_factory=dict)
    counters: dict[str, dict[str, int]] = field(default_factory=dict)


def load_sources(path: Path) -> dict[str, UkConfig]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(inn): UkConfig(**(value or {})) for inn, value in raw.items()}


def collection_cutoff(as_of: date, months: int) -> date:
    return as_of - timedelta(days=months * 31)


async def _collect_uk(
    uk: ManagementCompany,
    settings: Settings,
    registry: Registry,
    configs: dict[str, UkConfig],
    fetcher: Fetcher,
    series: SeriesStore,
    official: OfficialStore,
    result: CollectResult,
    gate: asyncio.Semaphore,
) -> None:
    funds = registry.funds_of(uk.inn)
    config = configs.get(uk.inn, UkConfig())
    ctx = CollectContext(
        uk, funds, config, fetcher, series, official, collection_cutoff(registry.as_of, settings.history_months)
    )
    bound = structlog.contextvars.bound_contextvars(uk_inn=uk.inn, uk=uk.short_name)
    async with gate:
        with bound, tracer().start_as_current_span("collect.uk") as span:
            span.set_attribute("pif.uk_inn", uk.inn)
            span.set_attribute("pif.uk", uk.short_name)
            span.set_attribute("pif.adapter", config.adapter)
            span.set_attribute("pif.funds", len(funds))
            adapter = ADAPTERS[config.adapter]()
            try:
                await asyncio.wait_for(adapter.collect(ctx), settings.adapter_timeout_s)
            except TimeoutError:
                reason = f"обход не уложился в {int(settings.adapter_timeout_s)} с"
                ctx.issues.append(Issue("warning", "fetch", uk.inn, uk.site or "", reason))
                ctx.site_failure = ctx.site_failure or (DataStatus.SITE_UNAVAILABLE, reason)
            except (FetchError, ParseError) as exc:
                # Сломанный сайт одной УК не должен ронять весь сбор.
                severity = "error" if isinstance(exc, ParseError) else "warning"
                stage = "parse" if isinstance(exc, ParseError) else "fetch"
                ctx.issues.append(Issue(severity, stage, uk.inn, uk.site or "", str(exc)[:300]))
                ctx.site_failure = (status_of(exc), str(exc)[:200])
                span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            except Exception as exc:
                log.exception("adapter_crashed")
                ctx.issues.append(Issue("error", "parse", uk.inn, uk.site or "", f"{type(exc).__name__}: {exc}"[:300]))
                ctx.site_failure = (DataStatus.PARSE_ERROR, f"{type(exc).__name__}: {exc}"[:200])
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))

            if ctx.site_failure is None and ctx.first_failure and not ctx.counters.get("pages"):
                ctx.site_failure = ctx.first_failure
            result.outcomes.update(ctx.outcomes)
            result.issues.extend(ctx.issues)
            result.counters[uk.inn] = dict(ctx.counters)
            if ctx.site_failure:
                result.uk_failures[uk.inn] = ctx.site_failure
            # Контрольная точка: прерванный прогон не теряет уже собранное.
            series.save()
            official.save()
            fetcher.manifest.save()
            fetcher.cache.save()
            with_data = sum(1 for o in ctx.outcomes.values() if o.daily_points or o.doc_points)
            span.set_attribute("pif.funds_with_data", with_data)
            log.info("uk_collected", funds=len(funds), with_data=with_data, **dict(ctx.counters))


async def collect_all(
    settings: Settings,
    registry: Registry,
    fetcher: Fetcher,
    series: SeriesStore,
    official: OfficialStore,
    only: set[str] | None = None,
) -> CollectResult:
    configs = load_sources(settings.config_dir / "sources.yaml")
    result = CollectResult()
    gate = asyncio.Semaphore(settings.max_parallel_hosts)
    companies = [c for c in registry.companies.values() if not only or c.inn in only]
    # Крупные УК стартуют первыми: так суммарное время прогона короче.
    companies.sort(key=lambda c: -len(registry.funds_of(c.inn)))
    with tracer().start_as_current_span("collect") as span:
        span.set_attribute("pif.companies", len(companies))
        await asyncio.gather(
            *(
                _collect_uk(c, settings, registry, configs, fetcher, series, official, result, gate)
                for c in companies
            )
        )
    return result
