"""Выборка 15 случайных значений для сверки с источниками.

Выборка делается один раз и дальше не меняется, чтобы результаты сверки не терялись.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

from pif.analytics import Analysis
from pif.storage.manifest import Manifest

SAMPLE_SIZE = 15
COLUMNS = ["№", "Фонд", "Дата", "Показатель", "Наше значение", "В источнике", "Совпало", "Снимок", "Источник"]
LABELS = {"nav": "СЧА", "unit_price": "Пай"}


def _sample_size(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(encoding="utf-8", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def write_manual_sample(path: Path, analysis: Analysis, manifest: Manifest) -> None:
    if _sample_size(path) >= SAMPLE_SIZE:
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
    for number, (view, points) in enumerate(sample, 1):
        point = rng.choice(points[-60:])
        field = rng.choice(["nav", "unit_price"])
        value = point.nav if field == "nav" else point.unit_price
        record = manifest.get(point.source_id)
        rows.append(
            {
                "№": number,
                "Фонд": view.fund.short_name,
                "Дата": point.day.isoformat(),
                "Показатель": LABELS[field],
                "Наше значение": str(value),
                "В источнике": "",
                "Совпало": "",
                "Снимок": "",
                "Источник": record.url if record else "",
            }
        )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
