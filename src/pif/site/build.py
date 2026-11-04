"""Сборка статического сайта /pif/.

Во время работы сайта нет ни сервера, ни базы: только HTML, CSS, JS и JSON. Все ссылки
относительные, поэтому сборка работает из любой подпапки. Сборка детерминирована: без изменений
в данных результат совпадает байт в байт, время прогона в страницы не попадает.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from pif.analytics import Analysis, FundView
from pif.config import Settings
from pif.flows.compute import last_months, rolling_sum
from pif.models import STATUS_LABELS, DataStatus, Fund
from pif.observability import get_logger, tracer
from pif.site import charts
from pif.storage.manifest import Manifest
from pif.storage.series_store import safe_name
from pif.verify.yields import PERIODS

log = get_logger("site")

STATIC = Path(__file__).parent / "static"
SRAVNENIE = "https://algosphere.tech/sravnenie/"
RATING_COLUMNS = [
    "rule_number", "name", "type", "cbr_category", "asset_class", "uk", "uk_inn", "isin", "ticker", "currency",
    "nav_rub", "nav_date", "unit_price", "flow_1m_rub", "flow_3m_rub", "flow_12m_rub",
    "return_1m_pct", "return_3m_pct", "return_6m_pct", "return_12m_pct", "status",
]  # fmt: skip


def fmt_money(value: Decimal | float | None) -> str:
    return "—" if value is None else charts.compact(float(value))


def fmt_number(value: Decimal | float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    text = f"{float(value):,.{digits}f}".replace(",", " ").replace(".", ",")
    return text.replace("-", "−")


def fmt_pct(value: Decimal | float | None) -> str:
    if value is None:
        return "—"
    number = float(value)
    return f"{'+' if number > 0 else ''}{number:.2f}".replace(".", ",").replace("-", "−") + " %"


def fmt_date(value: date | str | None) -> str:
    if value is None or value == "":
        return "—"
    day = date.fromisoformat(value) if isinstance(value, str) else value
    return day.strftime("%d.%m.%Y")


def _sign(value: Decimal | float | None) -> str:
    if value is None or value == 0:
        return ""
    return "pos" if value > 0 else "neg"


def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("pif.site", "templates"),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        money=fmt_money, number=fmt_number, pct=fmt_pct, ddate=fmt_date, sign=_sign, month=charts.month_label,
        urlq=lambda v: quote(str(v), safe=""),
    )  # fmt: skip
    return env


def _fund_url(rule: str) -> str:
    return f"fund/{quote(rule, safe='')}/"


def _status_kind(status: DataStatus) -> str:
    if status == DataStatus.OK:
        return "ok"
    if status in (DataStatus.PARTIAL, DataStatus.FORMED_LATER, DataStatus.NOT_FORMED):
        return "partial"
    return "bad"


def _sort_key(value: Decimal | float | None) -> str:
    return "" if value is None else f"{float(value):.6f}"


def _rating_row(view: FundView) -> dict[str, Any]:
    fund = view.fund
    search = " ".join(filter(None, [fund.short_name, fund.name, fund.isin, fund.ticker, fund.rule_number, fund.uk_name]))
    return {
        "fund": fund,
        "url": _fund_url(fund.rule_number),
        "view": view,
        "search": search.lower(),
        "status_label": STATUS_LABELS[view.status],
        "status_kind": _status_kind(view.status),
        "keys": {
            "nav": _sort_key(view.nav_rub),
            "flow1": _sort_key(view.flow_1m),
            "flow3": _sort_key(view.flow_3m),
            "flow12": _sort_key(view.flow_12m),
            "ret1": _sort_key(view.returns.get(1)),
            "ret3": _sort_key(view.returns.get(3)),
            "ret6": _sort_key(view.returns.get(6)),
            "ret12": _sort_key(view.returns.get(12)),
        },
    }


def _rating_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(RATING_COLUMNS)
    for row in rows:
        fund: Fund = row["fund"]
        view: FundView = row["view"]
        writer.writerow(
            [
                fund.rule_number, fund.short_name, fund.fund_type.value, fund.cbr_category, fund.asset_class,
                fund.uk_name, fund.uk_inn, fund.isin or "", fund.ticker or "", fund.currency,
                view.nav_rub if view.nav_rub is not None else "", view.nav_day or "",
                view.price if view.price is not None else "",
                *[v if v is not None else "" for v in (view.flow_1m, view.flow_3m, view.flow_12m)],
                *[f"{view.returns[p]:.4f}" if view.returns.get(p) is not None else "" for p in PERIODS],
                STATUS_LABELS[view.status],
            ]
        )  # fmt: skip
    return "﻿" + buffer.getvalue()


def _fund_payload(view: FundView, manifest: Manifest) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """JSON для подсказок, таблица источников и CSV ряда с адресом источника у каждой точки."""
    sources: dict[str, int] = {}
    table: list[dict[str, Any]] = []
    points = []
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        ["date", "nav", "unit_price", "adjusted_unit_price", "currency", "nav_rub", "unit_price_rub", "kind", "flag",
         "source_url", "source_sha256"]
    )  # fmt: skip
    for point in view.series.points:
        record = manifest.get(point.source_id)
        if point.source_id not in sources:
            sources[point.source_id] = len(sources)
            table.append(
                {
                    "url": record.url if record else "",
                    "fmt": record.fmt if record else "",
                    "fetched_at": record.fetched_at[:10] if record else "",
                    "sha256": record.sha256 if record else point.source_id,
                    "points": 0,
                }
            )
        index = sources[point.source_id]
        table[index]["points"] += 1
        points.append(
            [
                point.day.isoformat(),
                None if point.nav is None else float(point.nav),
                None if point.unit_price is None else float(point.unit_price),
                None if point.adj_price is None else round(float(point.adj_price), 6),
                index,
                point.kind != "daily",
            ]
        )
        writer.writerow(
            [
                point.day, point.nav if point.nav is not None else "",
                point.unit_price if point.unit_price is not None else "",
                point.adj_price if point.adj_price is not None else "", point.currency,
                point.nav_rub if point.nav_rub is not None else "",
                point.price_rub if point.price_rub is not None else "",
                point.kind, point.flag, record.url if record else "", record.sha256 if record else "",
            ]
        )  # fmt: skip
    payload = {
        "currency": view.series.currency,
        "points": points,
        "sources": [{"url": s["url"], "fmt": s["fmt"], "date": s["fetched_at"]} for s in table],
    }
    return payload, table, "﻿" + buffer.getvalue()


def _fund_charts(view: FundView, months: list[str]) -> dict[str, str]:
    gaps = [(g.start, g.end) for g in view.series.gaps]
    nav = [charts.LinePoint(p.day, float(p.nav_rub)) for p in view.series.points if p.nav_rub is not None]
    price = [charts.LinePoint(p.day, float(p.adj_price)) for p in view.series.points if p.adj_price is not None]
    monthly = {f.month: f.flow_rub for f in view.flows}
    bars = [(charts.month_label(m), float(monthly[m]) if monthly.get(m) is not None else None) for m in months]
    return {
        "nav": charts.line_chart(nav, gaps, label="СЧА", unit="руб."),
        "price": charts.line_chart(price, gaps, label="Стоимость пая", unit=view.series.currency),
        "flows": charts.bar_chart(bars, label="Чистые притоки по месяцам", unit="руб."),
    }


def _uk_summary(analysis: Analysis) -> list[dict[str, Any]]:
    grouped: dict[str, list[FundView]] = defaultdict(list)
    for view in analysis.views.values():
        grouped[view.fund.uk_inn].append(view)
    summary = []
    for inn, views in grouped.items():
        company = analysis.registry.companies[inn]
        monthly = analysis.totals.by_uk.get(inn, {})
        covered = [v for v in views if v.nav_rub is not None]
        summary.append(
            {
                "company": company,
                "views": sorted(views, key=lambda v: -(float(v.nav_rub or 0))),
                "funds": len(views),
                "covered": len(covered),
                "nav": sum((v.nav_rub for v in covered), Decimal(0)),
                "monthly": monthly,
                "flow_1m": monthly.get(analysis.last_month),
                "flow_12m": sum(monthly.values(), Decimal(0)) if monthly else None,
            }
        )
    return sorted(summary, key=lambda s: -s["nav"])


def _segments(analysis: Analysis, key: str, table: dict[str, dict[str, Decimal]]) -> list[dict[str, Any]]:
    nav: Counter[str] = Counter()
    funds: Counter[str] = Counter()
    for view in analysis.views.values():
        name = {"type": view.fund.fund_type.value, "category": view.fund.cbr_category, "asset": view.fund.asset_class}[key]
        funds[name] += 1
        nav[name] += float(view.nav_rub or 0)
    total = sum(nav.values()) or 1.0
    rows = []
    for name, value in nav.most_common():
        monthly = table.get(name, {})
        rows.append(
            {
                "name": name,
                "funds": funds[name],
                "nav": value,
                "share": value / total * 100,
                "flow_1m": monthly.get(analysis.last_month),
                "flow_3m": rolling_sum(monthly, last_months(analysis.last_month, 3)),
                "flow_12m": sum(monthly.values(), Decimal(0)) if monthly else None,
            }
        )
    return rows


def build_site(settings: Settings, analysis: Analysis, manifest: Manifest, quality: dict[str, Any]) -> Path:
    with tracer().start_as_current_span("site.build") as span:
        dist = settings.dist_dir / settings.base_path.strip("/")
        if dist.exists():
            shutil.rmtree(dist)
        (dist / "assets").mkdir(parents=True)
        shutil.copytree(STATIC, dist / "assets", dirs_exist_ok=True)
        env = _environment()
        registry = analysis.registry
        status_counts = Counter(v.status for v in analysis.views.values())
        common = {
            "as_of": registry.as_of,
            "registry_url": registry.source_url,
            "funds_total": len(analysis.views),
            "funds_with_data": sum(1 for v in analysis.views.values() if v.series.points),
            "last_month": analysis.last_month,
            "sravnenie": SRAVNENIE,
            # Версия от содержимого: браузер обновит стили сразу после выкладки.
            "css_v": hashlib.sha256((STATIC / "app.css").read_bytes()).hexdigest()[:8],
            "js_v": hashlib.sha256((STATIC / "app.js").read_bytes()).hexdigest()[:8],
        }

        def render(template: str, target: Path, depth: int, **context: Any) -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            root = "../" * depth or "./"
            html = env.get_template(template).render(root=root, **common, **context)
            target.write_text(html, encoding="utf-8", newline="\n")

        views = sorted(analysis.views.values(), key=lambda v: (-float(v.nav_rub or -1), v.fund.short_name))
        rows = [_rating_row(v) for v in views]
        (dist / "data").mkdir()
        (dist / "data" / "rating.csv").write_text(_rating_csv(rows), encoding="utf-8", newline="\n")
        render(
            "rating.html",
            dist / "index.html",
            0,
            page="rating",
            rows=rows,
            uks=sorted({(v.fund.uk_inn, v.fund.uk_name) for v in views}, key=lambda t: t[1]),
            asset_classes=sorted({v.fund.asset_class for v in views}),
            categories=sorted({v.fund.cbr_category for v in views}),
            currencies=sorted({v.fund.currency for v in views}),
            types=["Открытый", "Интервальный", "Биржевой"],
        )

        for view in views:
            fund = view.fund
            payload, source_table, series_csv = _fund_payload(view, manifest)
            name = safe_name(fund.rule_number)
            (dist / "data" / "fund").mkdir(exist_ok=True)
            (dist / "data" / "fund" / f"{name}.json").write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8", newline="\n"
            )
            (dist / "data" / "fund" / f"{name}.csv").write_text(series_csv, encoding="utf-8", newline="\n")
            company = registry.companies[fund.uk_inn]
            month_rows = sorted(view.flows, key=lambda f: f.month, reverse=True)[:13]
            render(
                "fund.html",
                dist / "fund" / name / "index.html",
                2,
                page="fund",
                view=view,
                fund=fund,
                company=company,
                charts=_fund_charts(view, analysis.months),
                sources=source_table,
                flow_rows=month_rows,
                data_file=quote(name, safe=""),
                status_label=STATUS_LABELS[view.status],
                status_kind=_status_kind(view.status),
                recent=list(reversed(view.series.points[-12:])),
                source_by_id={s["sha256"][:16]: s for s in source_table},
            )

        companies = _uk_summary(analysis)
        for summary in companies:
            bars = [
                (charts.month_label(m), float(summary["monthly"][m]) if m in summary["monthly"] else None)
                for m in analysis.months
            ]
            render(
                "uk.html",
                dist / "uk" / summary["company"].inn / "index.html",
                2,
                page="uk",
                s=summary,
                flows_chart=charts.bar_chart(bars, label="Чистые притоки УК", unit="руб."),
                fund_url=_fund_url,
                status_labels=STATUS_LABELS,
                status_kind=_status_kind,
            )

        market_bars = [
            (charts.month_label(m), float(analysis.totals.market[m]) if m in analysis.totals.market else None)
            for m in analysis.months
        ]
        render(
            "market.html",
            dist / "market" / "index.html",
            1,
            page="market",
            flows_chart=charts.bar_chart(market_bars, label="Чистые притоки рынка", unit="руб."),
            by_type=_segments(analysis, "type", analysis.totals.by_type),
            by_category=_segments(analysis, "category", analysis.totals.by_category),
            by_asset=_segments(analysis, "asset", analysis.totals.by_asset_class),
            top_nav=companies[:10],
            top_flow=sorted(
                [c for c in companies if c["flow_12m"] is not None], key=lambda c: -c["flow_12m"]
            )[:10],
            nav_total=sum(c["nav"] for c in companies),
            market=analysis.totals.market,
            months=analysis.months,
        )
        render(
            "quality.html",
            dist / "quality" / "index.html",
            1,
            page="quality",
            q=quality,
            statuses=[(s, STATUS_LABELS[s], status_counts.get(s, 0), _status_kind(s)) for s in DataStatus],
            corrections=sorted(analysis.corrections, key=lambda c: (c.day, c.rule_number), reverse=True)[:300],
            corrections_total=len(analysis.corrections),
            names={v.fund.rule_number: v.fund.short_name for v in views},
            fund_url=_fund_url,
        )
        for report in ("quality_report.json", "corrections.csv", "manual_check.csv"):
            source = settings.data_dir / report
            if source.exists():
                shutil.copy(source, dist / "data" / report)
        shutil.copy(settings.manifest_path, dist / "data" / "sources.csv")
        span.set_attribute("pif.pages", len(views) + len(companies) + 3)
        log.info("site_built", path=str(dist), funds=len(views), companies=len(companies))
        return dist
