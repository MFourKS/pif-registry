"""Серверная отрисовка графиков в SVG.

График приходит в готовом HTML и виден без JavaScript. Подсказки при наведении добавляет app.js
по тем же точкам, поэтому внешняя библиотека графиков не нужна.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from html import escape

WIDTH, HEIGHT = 760, 280
PAD_L, PAD_R, PAD_T, PAD_B = 62, 14, 14, 30
_MONTHS = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


@dataclass(frozen=True, slots=True)
class LinePoint:
    day: date
    value: float


def _nice_step(span: float, ticks: int = 4) -> float:
    if span <= 0:
        return 1.0
    raw = span / ticks
    magnitude = 10 ** __import__("math").floor(__import__("math").log10(raw))
    for multiplier in (1, 2, 2.5, 5, 10):
        if raw <= multiplier * magnitude:
            return multiplier * magnitude
    return 10 * magnitude


def compact(value: float) -> str:
    sign = "−" if value < 0 else ""
    value = abs(value)
    for limit, suffix in ((1e12, " трлн"), (1e9, " млрд"), (1e6, " млн"), (1e3, " тыс")):
        if value >= limit:
            scaled = value / limit
            text = f"{scaled:.1f}".rstrip("0").rstrip(".") if scaled < 100 else f"{scaled:.0f}"
            return f"{sign}{text.replace('.', ',')}{suffix}"
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{sign}{text.replace('.', ',')}"


def _y_scale(low: float, high: float) -> tuple[float, float, list[float]]:
    if low == high:
        low, high = low * 0.95 or -1, high * 1.05 or 1
    step = _nice_step(high - low)
    start = (low // step) * step
    end = -((-high) // step) * step
    ticks = []
    value = start
    while value <= end + step / 2:
        ticks.append(value)
        value += step
    return start, end, ticks


def line_chart(points: Sequence[LinePoint], gaps: Sequence[tuple[date, date]], *, label: str, unit: str) -> str:
    """Линия с разрывами на пропусках: соединять точки через дыру значит придумывать данные."""
    if len(points) < 2:
        return '<p class="muted">Недостаточно точек для графика.</p>'
    low, high, ticks = _y_scale(min(p.value for p in points), max(p.value for p in points))
    first, last = points[0].day, points[-1].day
    span_days = max((last - first).days, 1)
    plot_w, plot_h = WIDTH - PAD_L - PAD_R, HEIGHT - PAD_T - PAD_B

    def x(day: date) -> float:
        return PAD_L + plot_w * (day - first).days / span_days

    def y(value: float) -> float:
        return PAD_T + plot_h * (1 - (value - low) / (high - low))

    breaks = {right for _, right in gaps}
    segments: list[list[str]] = [[]]
    for point in points:
        if point.day in breaks and segments[-1]:
            segments.append([])
        segments[-1].append(f"{x(point.day):.1f},{y(point.value):.1f}")

    grid = "".join(
        f'<line class="grid" x1="{PAD_L}" x2="{WIDTH - PAD_R}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
        f'<text class="axis" x="{PAD_L - 8}" y="{y(t) + 4:.1f}" text-anchor="end">{escape(compact(t))}</text>'
        for t in ticks
    )
    months = []
    cursor = date(first.year, first.month, 1)
    every = max(1, round(span_days / 30 / 6))
    index = 0
    while cursor <= last:
        if cursor >= first and index % every == 0:
            text = f"{_MONTHS[cursor.month - 1]} {cursor.year % 100:02d}"
            months.append(f'<text class="axis" x="{x(cursor):.1f}" y="{HEIGHT - 8}" text-anchor="middle">{text}</text>')
        index += 1
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    lines = "".join(
        f'<polyline class="line" points="{" ".join(seg)}"/>'
        if len(seg) > 1
        else f'<circle class="dot" cx="{seg[0].split(",")[0]}" cy="{seg[0].split(",")[1]}" r="2.5"/>'
        for seg in segments
        if seg
    )
    gap_marks = "".join(
        f'<rect class="gap" x="{x(a):.1f}" y="{PAD_T}" width="{max(x(b) - x(a), 1):.1f}" height="{plot_h}"/>'
        for a, b in gaps
    )
    title = escape(f"{label}, {unit}: от {compact(points[0].value)} до {compact(points[-1].value)}")
    return (
        f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="{title}" '
        f'data-x0="{PAD_L}" data-x1="{WIDTH - PAD_R}">'
        f"<title>{title}</title>{grid}{gap_marks}{months and ''.join(months)}{lines}</svg>"
    )


def bar_chart(items: Sequence[tuple[str, float | None]], *, label: str, unit: str) -> str:
    """Столбики притоков. Месяц без данных остаётся пустым и подписан, а не рисуется нулём."""
    known = [value for _, value in items if value is not None]
    if not known:
        return '<p class="muted">Притоки не рассчитаны: нет двух соседних месячных точек.</p>'
    low, high, ticks = _y_scale(min(0.0, *known), max(0.0, *known))
    plot_w, plot_h = WIDTH - PAD_L - PAD_R, HEIGHT - PAD_T - PAD_B
    slot = plot_w / len(items)
    bar = slot * 0.62

    def y(value: float) -> float:
        return PAD_T + plot_h * (1 - (value - low) / (high - low))

    grid = "".join(
        f'<line class="grid" x1="{PAD_L}" x2="{WIDTH - PAD_R}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
        f'<text class="axis" x="{PAD_L - 8}" y="{y(t) + 4:.1f}" text-anchor="end">{escape(compact(t))}</text>'
        for t in ticks
    )
    bars = []
    every = max(1, round(len(items) / 7))
    for index, (name, value) in enumerate(items):
        center = PAD_L + slot * (index + 0.5)
        if value is None:
            bars.append(f'<text class="axis nodata" x="{center:.1f}" y="{y(0) - 4:.1f}" text-anchor="middle">н/д</text>')
        else:
            top, bottom = (y(value), y(0)) if value >= 0 else (y(0), y(value))
            kind = "pos" if value >= 0 else "neg"
            bars.append(
                f'<rect class="bar {kind}" x="{center - bar / 2:.1f}" y="{top:.1f}" width="{bar:.1f}" '
                f'height="{max(bottom - top, 1):.1f}"><title>{escape(name)}: {escape(compact(value))} {unit}</title></rect>'
            )
        if index % every == 0:
            bars.append(f'<text class="axis" x="{center:.1f}" y="{HEIGHT - 8}" text-anchor="middle">{escape(name)}</text>')
    return (
        f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="{escape(label)}">'
        f"<title>{escape(label)}</title>{grid}"
        f'<line class="zero" x1="{PAD_L}" x2="{WIDTH - PAD_R}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>{"".join(bars)}</svg>'
    )


def month_label(month: str) -> str:
    year, number = month.split("-")
    return f"{_MONTHS[int(number) - 1]} {year[2:]}"
