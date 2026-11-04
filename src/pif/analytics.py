"""Этап расчётов: нормализация, притоки, статусы, доходность, витрина данных для сайта."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from pif.adapters.base import FundOutcome, Issue
from pif.collect import CollectResult
from pif.config import Settings
from pif.flows.compute import (
    FlowTotals,
    MonthlyFlow,
    TieOut,
    aggregate,
    check_tie_out,
    fund_flows,
    last_months,
    rolling_sum,
)
from pif.models import STATUS_LABELS, DataStatus, Fund
from pif.normalize.fx import FxRates
from pif.normalize.series import (
    Correction,
    NormalizedSeries,
    Reconciliation,
    documents_only,
    last_month_end,
    month_end_points,
    month_key,
    normalize,
    reconcile_with_documents,
    repeated_documents,
)
from pif.observability import get_logger, tracer
from pif.registry.build import Registry
from pif.storage.manifest import Manifest
from pif.storage.series_store import OfficialStore, Revision, SeriesStore
from pif.verify.yields import PERIODS, YieldCheck, check_fund, computed_return

log = get_logger("analytics")

WINDOW_MONTHS = 12


@dataclass(slots=True)
class FundView:
    """Всё, что нужно сайту и отчётам об одном фонде."""

    fund: Fund
    series: NormalizedSeries
    status: DataStatus
    status_reason: str
    flows: list[MonthlyFlow] = field(default_factory=list)
    nav_rub: Decimal | None = None
    nav_day: date | None = None
    price: Decimal | None = None
    returns: dict[int, Decimal | None] = field(default_factory=dict)
    flow_1m: Decimal | None = None
    flow_3m: Decimal | None = None
    flow_12m: Decimal | None = None
    checks: list[YieldCheck] = field(default_factory=list)
    mismatches: list[YieldCheck] = field(default_factory=list)
    pays_income: bool | None = None


@dataclass(slots=True)
class Analysis:
    registry: Registry
    views: dict[str, FundView]
    totals: FlowTotals
    tie_out: list[TieOut]
    corrections: list[Correction]
    last_month: str
    months: list[str]
    conflicts: list[tuple[str, Reconciliation]] = field(default_factory=list)


def _window_start(as_of: date) -> date:
    return date(as_of.year - 1, as_of.month, 1)


def classify_status(
    fund: Fund,
    series: NormalizedSeries,
    outcome: FundOutcome | None,
    uk_failure: tuple[DataStatus, str] | None,
    as_of: date,
) -> tuple[DataStatus, str]:
    """Статус описывает данные, а не прогон. Причина показывается рядом с фондом."""
    points = series.points
    window_start = _window_start(as_of)
    if fund.cbr_status == "Формируется" and not points:
        return DataStatus.NOT_FORMED, "По реестру ЦБ фонд ещё формируется, стоимость пая не рассчитывается."
    if not points:
        if outcome and outcome.failure:
            return outcome.failure, outcome.failure_reason
        if uk_failure:
            return uk_failure
        if fund.for_qualified:
            return DataStatus.NOT_FOUND, (
                "Фонд для квалифицированных инвесторов: на открытых страницах сайта УК ни таблицы СЧА, ни КИД нет. "
                "Закрытые разделы не обходим."
            )
        return DataStatus.NOT_FOUND, "На сайте УК не найдено ни таблицы СЧА, ни КИД этого фонда."

    first = points[0].day
    formed_late = fund.formed_on is not None and fund.formed_on > window_start
    if first <= window_start + _GRACE or len(month_end_points(points)) >= WINDOW_MONTHS:
        if series.gaps:
            gap = max(series.gaps, key=lambda g: g.days)
            return DataStatus.PARTIAL, f"Есть пропуски в ряду, самый длинный: {gap.start} — {gap.end}."
        return DataStatus.OK, ""
    if formed_late:
        return DataStatus.FORMED_LATER, f"Фонд сформирован {fund.formed_on:%d.%m.%Y}, раньше данных нет."
    if len(points) < 3:
        return DataStatus.NO_HISTORY, "УК публикует только текущие значения, архива за 12 месяцев на сайте нет."
    return DataStatus.PARTIAL, f"Источник отдаёт ряд только с {first:%d.%m.%Y}."


_GRACE = date(2000, 2, 15) - date(2000, 1, 1)


def _latest_nav(series: NormalizedSeries) -> tuple[Decimal | None, Decimal | None, date | None]:
    for point in reversed(series.points):
        if point.nav_rub is not None:
            return point.nav_rub, point.unit_price, point.day
    return None, None, None


def build_analysis(
    settings: Settings,
    registry: Registry,
    series_store: SeriesStore,
    official: OfficialStore,
    fx: FxRates,
    collected: CollectResult | None,
) -> Analysis:
    with tracer().start_as_current_span("analytics"):
        as_of = registry.as_of
        last_complete = last_month_end(as_of)
        last_month = month_key(last_complete)
        months = last_months(last_month, WINDOW_MONTHS)
        not_before = date(as_of.year - 2, as_of.month, 1)

        by_rule_official: dict[str, list[Any]] = defaultdict(list)
        for record in official.rows.values():
            by_rule_official[record.rule_number].append(record)

        views: dict[str, FundView] = {}
        corrections: list[Correction] = []
        conflicts: list[tuple[str, Reconciliation]] = []
        all_flows: list[MonthlyFlow] = []
        for fund in registry.funds:
            documents = official.points_of(fund.rule_number)
            repeated = repeated_documents(documents)
            documents = [d for d in documents if d.day not in repeated]
            observed = {
                day: point
                for day, point in series_store.load(fund.rule_number).items()
                if not (day in repeated and point.kind != "daily")
            }
            verdict = reconcile_with_documents(observed, documents)
            if verdict.rejected:
                # Ряд чужого фонда: на сайте остаются только значения из КИД этого фонда.
                observed = documents_only(documents)
            series = normalize(fund.rule_number, observed, fx, not_before)
            if verdict.rejected:
                series.corrections.append(
                    Correction(
                        fund.rule_number,
                        verdict.worst_day or as_of,
                        "series",
                        f"дневной ряд, расхождений с КИД: {verdict.mismatched} из {verdict.compared}",
                        "значения из КИД",
                        "Дневной ряд не совпал с КИД фонда: страница сопоставлена неверно, ряд отброшен",
                        "",
                    )
                )
            elif verdict.mismatched:
                conflicts.append((fund.rule_number, verdict))
            series.corrections.extend(
                Correction(
                    fund.rule_number,
                    day,
                    "series",
                    day.isoformat(),
                    real.isoformat(),
                    f"КИД повторяет значения на {real:%d.%m.%Y}: это переиздание или ошибка в дате, точка убрана",
                    "",
                )
                for day, real in sorted(repeated.items())
            )
            fund.currency = series.currency
            outcome = collected.outcomes.get(fund.rule_number) if collected else None
            uk_failure = collected.uk_failures.get(fund.uk_inn) if collected else None
            status, reason = classify_status(fund, series, outcome, uk_failure, as_of)
            view = FundView(fund, series, status, reason)
            view.flows = fund_flows(fund, series, fx, last_complete)
            view.nav_rub, view.price, view.nav_day = _latest_nav(series)
            monthly = {f.month: f.flow_rub for f in view.flows if f.flow_rub is not None}
            view.flow_1m = rolling_sum(monthly, last_months(last_month, 1))
            view.flow_3m = rolling_sum(monthly, last_months(last_month, 3))
            view.flow_12m = rolling_sum(monthly, last_months(last_month, 12))
            ends = month_end_points(series.points)
            basis = ends.get(last_month)
            for period in PERIODS:
                value = computed_return(series.points, basis.day, period)[0] if basis else None
                view.returns[period] = value
            view.pays_income = next((d.pays_income for d in reversed(documents) if d.pays_income is not None), None)
            view.checks = check_fund(
                series, [r for r in by_rule_official.get(fund.rule_number, []) if r.report_date not in repeated]
            )
            tolerance = Decimal(str(settings.yield_tolerance_pp))
            view.mismatches = [
                c for c in view.checks if c.difference_pp is not None and abs(c.difference_pp) > tolerance
            ]
            views[fund.rule_number] = view
            corrections.extend(series.corrections)
            all_flows.extend(view.flows)

        window_flows = [f for f in all_flows if f.month in set(months)]
        totals = aggregate(registry.funds, window_flows)
        tie_out = check_tie_out(totals)
        status_counts = Counter(v.status.value for v in views.values())
        log.info(
            "analysis_done",
            funds=len(views),
            flows=len(window_flows),
            corrections=len(corrections),
            tie_out_breaks=len(tie_out),
            **status_counts,
        )
        return Analysis(registry, views, totals, tie_out, corrections, last_month, months, conflicts)


def write_corrections(path: Path, corrections: list[Correction], revisions: list[Revision]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(corrections, key=lambda c: (c.rule_number, c.day, c.field))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["rule_number", "date", "field", "old_value", "new_value", "reason", "source_id"])
        for c in rows:
            writer.writerow([c.rule_number, c.day.isoformat(), c.field, c.old_value, c.new_value, c.reason, c.source_id])
        for r in sorted(revisions, key=lambda r: (r.rule_number, r.day, r.field)):
            writer.writerow(
                [r.rule_number, r.day.isoformat(), r.field, r.old, r.new, "Источник изменил опубликованное значение", r.source_id]
            )


def write_flows(path: Path, analysis: Analysis) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            ["rule_number", "month", "start_day", "end_day", "nav_start", "nav_end", "price_start", "price_end",
             "flow", "market_effect", "flow_rub", "kind"]
        )  # fmt: skip
        for rule in sorted(analysis.views):
            for f in analysis.views[rule].flows:
                writer.writerow(
                    [f.rule_number, f.month, f.start_day or "", f.end_day, f.nav_start or "", f.nav_end,
                     f.price_start or "", f.price_end, f.flow, f.market_effect, f.flow_rub or "", f.kind]
                )  # fmt: skip


_PAYOUT_NAME = re.compile(r"(?i)выплат|рантье|купонн|дивиденд|поток|ежемесячн\w* доход|денежный доход")


def mismatch_reason(view: FundView, check: YieldCheck) -> str:
    """Объяснение расхождения. Там, где причины не знаем, так и пишем."""
    difference = check.difference_pp or Decimal(0)
    pays = view.pays_income if view.pays_income is not None else bool(_PAYOUT_NAME.search(view.fund.name))
    if pays and difference < 0:
        return "Фонд выплачивает доход: официальная доходность учитывает выплаты, а ряд пая их не содержит."
    return "Причина не установлена."


def quality_report(
    analysis: Analysis,
    issues: list[Issue],
    settings: Settings,
    manifest: Manifest,
) -> dict[str, Any]:
    views = analysis.views.values()
    statuses = Counter(v.status.value for v in views)
    known = load_known_issues(settings.config_dir / "known_issues.yaml")
    all_errors = [i for i in issues if i.severity == "error"]
    errors = [i for i in all_errors if _known_reason(i, known) is None]
    acknowledged = [
        {**_issue(i), "reason": _known_reason(i, known)} for i in all_errors if _known_reason(i, known) is not None
    ]
    mismatches = [
        {
            "fund": c.rule_number,
            "name": analysis.views[c.rule_number].fund.short_name,
            "period_months": c.months,
            "report_date": c.report_date.isoformat(),
            "official_pct": str(c.official_pct),
            "computed_pct": f"{c.computed_pct:.2f}",
            "difference_pp": f"{c.difference_pp:.2f}",
            "source_id": c.source_id,
            "source_url": record.url if (record := manifest.get(c.source_id)) else "",
            "reason": mismatch_reason(analysis.views[c.rule_number], c),
        }
        for v in views
        for c in v.mismatches
    ]
    all_checks = [c for v in views for c in v.checks]
    compared = [c for c in all_checks if c.computed_pct is not None]
    return {
        "registry_as_of": analysis.registry.as_of.isoformat(),
        "funds_total": len(analysis.views),
        "statuses": {
            status.value: {"label": STATUS_LABELS[status], "count": statuses.get(status.value, 0)}
            for status in DataStatus
        },
        "funds_with_series": sum(1 for v in views if v.series.points),
        "retail_funds": sum(1 for v in views if not v.fund.for_qualified),
        "retail_funds_with_series": sum(1 for v in views if v.series.points and not v.fund.for_qualified),
        "points_total": sum(len(v.series.points) for v in views),
        "errors": [_issue(i) for i in errors],
        "known_issues": acknowledged,
        "warnings_total": sum(1 for i in issues if i.severity == "warning"),
        "warnings_by_stage": dict(Counter(i.stage for i in issues if i.severity == "warning")),
        "yield_check": {
            "tolerance_pp": settings.yield_tolerance_pp,
            "official_values": len(all_checks),
            "compared": len(compared),
            "not_comparable": len(all_checks) - len(compared),
            "within_tolerance": len(compared) - len(mismatches),
            "mismatches": sorted(mismatches, key=lambda m: -abs(float(m["difference_pp"]))),
        },
        "corrections_total": len(analysis.corrections),
        "kid_conflicts": [
            {
                "fund": rule,
                "compared": verdict.compared,
                "mismatched": verdict.mismatched,
                "worst_day": verdict.worst_day.isoformat() if verdict.worst_day else None,
                "worst_pct": round(verdict.worst_ratio * 100, 3),
            }
            for rule, verdict in analysis.conflicts
        ],
        "tie_out_breaks": [
            {"level": t.level, "month": t.month, "difference": str(t.difference)} for t in analysis.tie_out
        ],
        "publish_allowed": not errors and not analysis.tie_out,
    }


def load_known_issues(path: Path) -> list[tuple[re.Pattern[str], str]]:
    """Разобранные вручную ошибки. Новая ошибка публикацию запретит, известная останется в отчёте."""
    if not path.exists():
        return []
    items = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [(re.compile(item["url"]), item["reason"]) for item in items]


def _known_reason(issue: Issue, known: list[tuple[re.Pattern[str], str]]) -> str | None:
    return next((reason for pattern, reason in known if pattern.search(issue.url)), None)


def _issue(issue: Issue) -> dict[str, str]:
    return {
        "stage": issue.stage,
        "uk_inn": issue.uk_inn,
        "url": issue.url,
        "fund": issue.fund,
        "message": issue.message,
    }


def write_quality(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
