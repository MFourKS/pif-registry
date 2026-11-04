"""Выборка 15 случайных значений для ручной сверки с сайтами УК.

Выборка фиксирована зерном и датой реестра: повторный прогон на тех же данных даёт те же строки.
Уже заполненные результаты проверки при пересборке сохраняются.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

from pif.analytics import Analysis
from pif.storage.manifest import Manifest

SAMPLE_SIZE = 15
COLUMNS = [
    "rule_number", "fund", "uk", "date", "field", "our_value", "source_url", "seen_value", "matches", "checked_by",
    "checked_at", "comment",
]  # fmt: skip


def _load_existing(path: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as handle:
        return {(r["rule_number"], r["date"], r["field"]): r for r in csv.DictReader(handle)}


def write_manual_sample(path: Path, analysis: Analysis, manifest: Manifest) -> None:
    existing = _load_existing(path)
    filled = [row for row in existing.values() if row.get("checked_by")]
    if len(filled) >= SAMPLE_SIZE:
        return

    candidates = []
    for rule in sorted(analysis.views):
        view = analysis.views[rule]
        recent = [p for p in view.series.points if p.nav is not None and p.unit_price is not None]
        if recent:
            candidates.append((view, recent))

    rng = random.Random(f"pif-manual-{analysis.registry.as_of.isoformat()}")
    sample = rng.sample(candidates, min(SAMPLE_SIZE, len(candidates)))
    rows = []
    for view, points in sample:
        point = rng.choice(points[-60:])
        field = rng.choice(["nav", "unit_price"])
        value = point.nav if field == "nav" else point.unit_price
        record = manifest.get(point.source_id)
        key = (view.fund.rule_number, point.day.isoformat(), field)
        row = existing.get(key, {})
        rows.append(
            {
                "rule_number": view.fund.rule_number,
                "fund": view.fund.short_name,
                "uk": view.fund.uk_name,
                "date": point.day.isoformat(),
                "field": field,
                "our_value": str(value),
                "source_url": record.url if record else "",
                "seen_value": row.get("seen_value", ""),
                "matches": row.get("matches", ""),
                "checked_by": row.get("checked_by", ""),
                "checked_at": row.get("checked_at", ""),
                "comment": row.get("comment", ""),
            }
        )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
