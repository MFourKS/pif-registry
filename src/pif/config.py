"""Настройки конвейера. Значения по умолчанию рассчитаны на запуск одной командой."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Честная подпись: кто мы и зачем. Слово bot и адрес в подписи режутся WAF части сайтов УК,
# поэтому формулировка простая, но имя инструмента и цель остаются открытыми.
USER_AGENT = "pif-registry/0.1 (Algosphere test assignment; public fund disclosures; 1 req/s per host)"


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path = ROOT
    data_dir: Path = ROOT / "data"
    raw_dir: Path = ROOT / "raw"
    state_dir: Path = ROOT / "state"
    out_dir: Path = ROOT / "out"
    dist_dir: Path = ROOT / "dist"
    config_dir: Path = ROOT / "config"
    manifest_path: Path = ROOT / "manifest" / "sources.csv"

    user_agent: str = USER_AGENT
    # Требование задания: к чужим сайтам не чаще одного запроса в секунду на хост.
    min_host_interval_s: float = 1.05
    # Сколько хостов обходим одновременно: хосты независимы, поэтому лимит на хост не нарушается.
    max_parallel_hosts: int = 16
    request_timeout_s: float = 45.0
    retries: int = 2
    # Строгий режим: robots.txt действует и на прямые ссылки на файлы раскрытия. См. docs/QUESTIONS.md.
    strict_robots: bool = field(default_factory=lambda: os.environ.get("PIF_STRICT_ROBOTS") == "1")
    # Окно свежести ответа: внутри него повторный запуск не ходит в сеть вовсе.
    freshness_ttl_s: int = 20 * 3600
    # Обход одной УК ограничен, чтобы зависший сайт не держал весь прогон.
    adapter_timeout_s: float = 45 * 60

    history_months: int = 15
    # Порог из задания: расхождение с официальной доходностью, п. п.
    yield_tolerance_pp: float = 0.5
    base_path: str = field(default_factory=lambda: os.environ.get("PIF_BASE_PATH", "/pif/"))
    as_of: date | None = None

    @property
    def series_dir(self) -> Path:
        return self.data_dir / "series"

    @property
    def official_dir(self) -> Path:
        return self.data_dir / "official"


def load_settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]
