"""Накопленные наблюдения: data/series/<номер правил>.csv и data/official/returns.csv.

Это состояние проекта, оно лежит в git: именно поэтому обновление докачивает только новые точки.
Значения хранятся текстом ровно в том виде, как их опубликовала УК.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from pif.models import Observation, OfficialReturn

_COLUMNS = ["date", "nav", "unit_price", "currency", "kind", "source_id"]
# Дневной ряд точнее месячного документа: в КИД значения бывают округлены до млн.
_KIND_PRIORITY = {"daily": 2, "monthly_doc": 1}


def safe_name(rule_number: str) -> str:
    return rule_number.replace("/", "_")


@dataclass(frozen=True, slots=True)
class Revision:
    """Источник изменил уже опубликованное значение: это нужно видеть в журнале."""

    rule_number: str
    day: date
    field: str
    old: str
    new: str
    source_id: str


class SeriesStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, dict[date, Observation]] = {}
        self.revisions: list[Revision] = []
        self.dirty: set[str] = set()

    def _path(self, rule_number: str) -> Path:
        return self.root / f"{safe_name(rule_number)}.csv"

    def load(self, rule_number: str) -> dict[date, Observation]:
        if rule_number in self._cache:
            return self._cache[rule_number]
        points: dict[date, Observation] = {}
        path = self._path(rule_number)
        if path.exists():
            with path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    day = date.fromisoformat(row["date"])
                    points[day] = Observation(
                        day=day,
                        nav=Decimal(row["nav"]) if row["nav"] else None,
                        unit_price=Decimal(row["unit_price"]) if row["unit_price"] else None,
                        currency=row["currency"],
                        kind=row["kind"],
                        source_id=row["source_id"],
                    )
        self._cache[rule_number] = points
        return points

    def last_date(self, rule_number: str) -> date | None:
        points = self.load(rule_number)
        return max(points) if points else None

    def add(self, rule_number: str, new: Observation) -> bool:
        points = self.load(rule_number)
        current = points.get(new.day)
        if current is None:
            points[new.day] = new
            self.dirty.add(rule_number)
            return True

        merged = self._merge(rule_number, current, new)
        if merged != current:
            points[new.day] = merged
            self.dirty.add(rule_number)
            return True
        return False

    def _merge(self, rule_number: str, current: Observation, new: Observation) -> Observation:
        new_rank, old_rank = _KIND_PRIORITY.get(new.kind, 0), _KIND_PRIORITY.get(current.kind, 0)
        if new_rank < old_rank:
            # Менее точный источник только заполняет пустоты, например СЧА к ряду стоимости пая.
            return Observation(
                current.day,
                current.nav if current.nav is not None else new.nav,
                current.unit_price if current.unit_price is not None else new.unit_price,
                current.currency,
                current.kind,
                current.source_id,
            )
        for field in ("nav", "unit_price"):
            old_value, new_value = getattr(current, field), getattr(new, field)
            if old_value is not None and new_value is not None and old_value != new_value:
                self.revisions.append(
                    Revision(rule_number, current.day, field, str(old_value), str(new_value), new.source_id)
                )
        if (new.nav is None and current.nav is not None) or (
            new.unit_price is None and current.unit_price is not None
        ):
            new = Observation(
                new.day,
                new.nav if new.nav is not None else current.nav,
                new.unit_price if new.unit_price is not None else current.unit_price,
                new.currency,
                new.kind,
                new.source_id,
            )
        # Тот же набор значений из более свежей копии страницы не меняет ссылку на источник,
        # иначе повторный прогон переписывал бы файлы без причины.
        if (new.nav, new.unit_price, new.kind) == (current.nav, current.unit_price, current.kind):
            return current
        return new

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for rule_number in sorted(self.dirty):
            points = self._cache[rule_number]
            path = self._path(rule_number)
            tmp = path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, lineterminator="\n")
                writer.writerow(_COLUMNS)
                for day in sorted(points):
                    p = points[day]
                    writer.writerow(
                        [
                            day.isoformat(),
                            "" if p.nav is None else str(p.nav),
                            "" if p.unit_price is None else str(p.unit_price),
                            p.currency,
                            p.kind,
                            p.source_id,
                        ]
                    )
            tmp.replace(path)
        self.dirty.clear()


class OfficialStore:
    """Официальная доходность из КИД: один файл на все фонды."""

    COLUMNS = ("rule_number", "report_date", "months", "value_pct", "source_id")

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: dict[tuple[str, date, int], OfficialReturn] = {}
        if path.exists():
            with path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    record = OfficialReturn(
                        row["rule_number"],
                        date.fromisoformat(row["report_date"]),
                        int(row["months"]),
                        Decimal(row["value_pct"]),
                        row["source_id"],
                    )
                    self.rows[(record.rule_number, record.report_date, record.months)] = record

    def add(self, record: OfficialReturn) -> None:
        self.rows[(record.rule_number, record.report_date, record.months)] = record

    def of_fund(self, rule_number: str) -> list[OfficialReturn]:
        return [r for r in self.rows.values() if r.rule_number == rule_number]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(self.COLUMNS)
            for key in sorted(self.rows):
                r = self.rows[key]
                writer.writerow([r.rule_number, r.report_date.isoformat(), r.months, str(r.value_pct), r.source_id])
