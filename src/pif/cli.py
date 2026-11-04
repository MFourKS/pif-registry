"""Командная строка. Весь путь от сбора до готового сайта: `pif run`."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

import typer

from pif.analytics import build_analysis, quality_report, write_corrections, write_flows, write_quality
from pif.collect import (
    CollectResult,
    collect_all,
    load_collect_state,
    load_sources,
    merge_partial,
    save_collect_state,
    without_geo_blocked,
)
from pif.config import Settings, load_settings
from pif.manual_check import write_manual_sample
from pif.net.client import Fetcher
from pif.normalize import fx as fx_module
from pif.observability import configure_logging, configure_tracing, get_logger, tracer
from pif.registry.build import Registry, build_registry
from pif.site.build import build_site
from pif.storage.manifest import Manifest
from pif.storage.rawstore import RawStore
from pif.storage.series_store import OfficialStore, SeriesStore

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Реестр розничных ПИФов России")
log = get_logger("cli")

EXIT_QUALITY_GATE = 3


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


async def _collect(
    settings: Settings, force: bool, only: set[str] | None, skip_collect: bool, skip_geo_blocked: bool = False
) -> tuple[Registry, SeriesStore, OfficialStore, fx_module.FxRates, CollectResult | None, Manifest, dict[str, object]]:
    raw = RawStore(settings.raw_dir)
    manifest = Manifest(settings.manifest_path)
    series = SeriesStore(settings.series_dir)
    official = OfficialStore(settings.official_dir / "returns.csv")
    registry_path = settings.data_dir / "registry.json"
    collected: CollectResult | None = None

    async with Fetcher(settings, raw, manifest, force=force) as fetcher:
        state_path = settings.data_dir / "collect_state.json"
        if skip_collect:
            registry = Registry.load(registry_path)
            collected = load_collect_state(state_path)
        else:
            registry = await build_registry(settings, fetcher)
            registry.save(registry_path)
            if skip_geo_blocked:
                configs = load_sources(settings.config_dir / "sources.yaml")
                only = without_geo_blocked(only or registry.companies, configs)
                log.info("geo_blocked_skipped", skipped=len(registry.companies) - len(only))
            collected = await collect_all(settings, registry, fetcher, series, official, only)
            if only:
                collected = merge_partial(load_collect_state(state_path), collected, only)
            save_collect_state(state_path, collected)

        currencies = set()
        for fund in registry.funds:
            currencies |= {p.currency for p in series.load(fund.rule_number).values() if p.currency}
        if skip_collect:
            rates = fx_module.FxRates.load(settings.data_dir / "fx")
        else:
            start = date(registry.as_of.year - 2, registry.as_of.month, 1)
            rates = await fx_module.fetch_rates(fetcher, currencies, start, registry.as_of)
            rates.save(settings.data_dir / "fx")
        stats = fetcher.stats
        fetch_stats: dict[str, object] = {
            "network_requests": stats.network,
            "not_modified": stats.not_modified,
            "served_from_fresh_cache": stats.fresh_cache,
            "errors": stats.errors,
            "robots_denied": stats.robots_denied,
            "robots_public_documents": stats.robots_public_documents,
            "bytes_downloaded": stats.bytes_downloaded,
            "tls_unverified_hosts": sorted(stats.tls_unverified_hosts),
        }
    series.save()
    official.save()
    manifest.save()
    return registry, series, official, rates, collected, manifest, fetch_stats


def _pipeline(
    settings: Settings,
    *,
    force: bool,
    only: set[str] | None,
    skip_collect: bool,
    build: bool,
    skip_geo_blocked: bool = False,
) -> int:
    run_id = _run_id()
    log_path = configure_logging(run_id, settings.out_dir / "logs")
    provider = configure_tracing(run_id, settings.out_dir / "traces")
    started = time.monotonic()
    log.info("run_started", run_id=run_id, force=force, skip_collect=skip_collect, log=str(log_path))
    try:
        with tracer().start_as_current_span("pipeline") as span:
            span.set_attribute("pif.run_id", run_id)
            registry, series, official, rates, collected, manifest, fetch_stats = asyncio.run(
                _collect(settings, force, only, skip_collect, skip_geo_blocked)
            )
            analysis = build_analysis(settings, registry, series, official, rates, collected)
            registry.save(settings.data_dir / "registry.json")

            issues = collected.issues if collected else []
            report = quality_report(analysis, issues, settings, manifest, collected.uk_failures if collected else None)
            write_quality(settings.data_dir / "quality_report.json", report)
            write_corrections(settings.data_dir / "corrections.csv", analysis.corrections, series.revisions)
            write_flows(settings.data_dir / "flows.csv", analysis)
            write_manual_sample(settings.data_dir / "manual_check.csv", analysis, manifest)

            summary = {
                "run_id": run_id,
                "duration_s": round(time.monotonic() - started, 1),
                "fetch": fetch_stats,
                "warnings": [asdict(i) for i in issues if i.severity == "warning"],
            }
            settings.out_dir.mkdir(parents=True, exist_ok=True)
            (settings.out_dir / "last_run.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
            )
            if build:
                dist = build_site(settings, analysis, manifest, report)
                log.info("site_ready", path=str(dist))

            errors = len(report["errors"]) + len(report["tie_out_breaks"])
            span.set_attribute("pif.errors", errors)
            log.info(
                "run_finished",
                funds=report["funds_total"],
                with_series=report["funds_with_series"],
                points=report["points_total"],
                errors=errors,
                mismatches=len(report["yield_check"]["mismatches"]),
                publish_allowed=report["publish_allowed"],
                seconds=summary["duration_s"],
                **{k: v for k, v in fetch_stats.items() if k in ("network_requests", "not_modified")},
            )
            return 0 if report["publish_allowed"] else EXIT_QUALITY_GATE
    finally:
        provider.shutdown()


@app.command()
def run(
    force: bool = typer.Option(False, help="Запросить всё заново, не глядя на окно свежести"),
    only: list[str] = typer.Option(None, help="Собрать только эти УК (ИНН)"),
    no_gate: bool = typer.Option(False, help="Вернуть код 0, даже если отчёт качества запрещает публикацию"),
    skip_geo_blocked: bool = typer.Option(False, help="Не обращаться к УК, закрытым для зарубежных адресов"),
) -> None:
    """Сбор, нормализация, притоки, отчёт качества и сборка сайта."""
    code = _pipeline(
        load_settings(),
        force=force,
        only=set(only) if only else None,
        skip_collect=False,
        build=True,
        skip_geo_blocked=skip_geo_blocked,
    )
    raise typer.Exit(0 if no_gate else code)


@app.command()
def build() -> None:
    """Пересчитать и собрать сайт из накопленных данных, без обращения к сайтам УК."""
    raise typer.Exit(_pipeline(load_settings(), force=False, only=None, skip_collect=True, build=True))


@app.command("fetch-raw")
def fetch_raw(limit: int = typer.Option(0, help="Скачать не больше стольких файлов, 0 — все")) -> None:
    """Докачать сырые файлы по манифесту и проверить их SHA-256."""
    settings = load_settings()
    configure_logging(_run_id(), settings.out_dir / "logs")
    raise typer.Exit(asyncio.run(_restore_raw(settings, limit)))


async def _restore_raw(settings: Settings, limit: int) -> int:
    from pif.storage.rawstore import sha256_of

    raw = RawStore(settings.raw_dir)
    manifest = Manifest(settings.manifest_path)
    missing = [r for r in manifest.records() if raw.get(r.sha256, r.fmt) is None]
    unique = list({r.sha256: r for r in missing}.values())
    if limit:
        unique = unique[:limit]
    changed = 0
    async with Fetcher(settings, raw, Manifest(settings.out_dir / "restore_manifest.csv"), force=True) as fetcher:
        for record in unique:
            result = await fetcher.get(record.url, public_document=True, retries=0)
            if sha256_of(result.content) != record.sha256:
                changed += 1
                log.warning("raw_changed_since_manifest", url=record.url)
    log.info("raw_restored", requested=len(unique), changed=changed)
    return 0


@app.command()
def gate(report: Path = typer.Option(Path("data/quality_report.json"))) -> None:
    """Проверка перед публикацией: код 0, только если нет ошибок сбора и разбора."""
    data = json.loads(report.read_text(encoding="utf-8"))
    if data["publish_allowed"]:
        typer.echo(f"Публикация разрешена. Расхождений с официальной доходностью: {len(data['yield_check']['mismatches'])}")
        raise typer.Exit(0)
    gap = data["collection_gap"]
    typer.echo(
        f"Публикация запрещена: ошибок {len(data['errors'])}, разрывов сверки {len(data['tie_out_breaks'])}, "
        f"не обновилось рядов {gap['funds_with_series']} ({gap['share']:.0%}): {', '.join(gap['companies'])}",
        err=True,
    )
    for error in data["errors"][:20]:
        typer.echo(f"  [{error['stage']}] {error['url']}: {error['message']}", err=True)
    raise typer.Exit(EXIT_QUALITY_GATE)


if __name__ == "__main__":
    sys.exit(app())
