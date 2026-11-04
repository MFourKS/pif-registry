"""Сопоставление названий фондов из документов УК с реестром Банка России.

Ключ фонда в проекте — номер правил. На сайтах УК он есть не всегда, поэтому сначала ищем номер,
а если его нет, сравниваем значимые слова названия с учётом прежних имён фонда. Неоднозначный
результат считается отсутствием совпадения: лучше честный пропуск, чем чужой ряд у фонда.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from pif.models import Fund, FundType

_BOILERPLATE = (
    r"открыт\w*|интервальн\w*|биржев\w*|закрыт\w*|паев\w*|инвестиционн\w*|фонд\w*|рыночных|финансовых|"
    r"инструментов|комбинированн\w*|опиф\w*|ипиф\w*|бпиф\w*|рфи|под|управлением|ооо|ао|пао|ук|"
    r"управляющ\w*|компани\w*|смешанных|инвестиций|акций|облигаций|фондов"
)
_FORMULA = re.compile(
    r"(?i)^\s*(открыт\w+|интервальн\w+|биржев\w+)\s+паев\w+\s+инвестиционн\w+\s+"
    r"(фонд\w*\s+)?((рыночных\s+финансовых\s+инструментов|смешанных\s+инвестиций|акций|облигаций|фондов|"
    r"комбинированн\w+)\s+)?(фонд\w*\s+)?"
)
_CLOSED_FUND = re.compile(r"(?i)\bзпиф|закрыт\w+\s+паев|заблокированн\w+\s+актив")
_QUOTED = re.compile(r"[«\"“„]([^»\"”“]{2,160})[»\"”“]")
_RULE = re.compile(r"\b(\d{4})\s*[–—-]\s*(\d{8}|\d{2}\s?\d{3}\s?\d{3}|СД)\b")
_TYPE_HINTS: tuple[tuple[str, FundType], ...] = (
    (r"(?i)\bопиф|открыт\w+\s+паев", FundType.OPEN),
    (r"(?i)\bипиф|интервальн\w+\s+паев", FundType.INTERVAL),
    (r"(?i)\bбпиф|биржев\w+\s+паев", FundType.EXCHANGE),
)


def _tokens(text: str, *, quoted: bool) -> frozenset[str]:
    lowered = text.lower().replace("ё", "е")
    words = re.findall(r"[a-zа-я0-9]+", lowered)
    if quoted:
        return frozenset(w for w in words if len(w) > 1 or w.isdigit())
    return frozenset(w for w in words if not re.fullmatch(_BOILERPLATE, w) and (len(w) > 1 or w.isdigit()))


def name_tokens(text: str) -> frozenset[str]:
    """Слова собственного имени фонда: содержимое кавычек или текст без служебных слов."""
    text = re.split(r"(?i)\bпод\s+управлением\b", text, maxsplit=1)[0]
    found = _QUOTED.findall(text)
    if found:
        return _tokens(found[-1], quoted=True)
    # Без кавычек отрезаем только типовое начало: в имени бывают слова вроде «Биржевая копилка».
    stripped = _FORMULA.sub("", text, count=1)
    if stripped != text:
        return _tokens(stripped, quoted=True)
    return _tokens(text, quoted=False)


def fund_type_hint(text: str) -> FundType | None:
    hints = {kind for pattern, kind in _TYPE_HINTS if re.search(pattern, text)}
    return hints.pop() if len(hints) == 1 else None


def rule_numbers_in(text: str) -> set[str]:
    return {f"{m[1]}-{re.sub(r'\s', '', m[2])}" for m in _RULE.finditer(text)}


@dataclass(frozen=True, slots=True)
class _Candidate:
    fund: Fund
    tokens: frozenset[str]


def _score(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    common = left & right
    if not common:
        return 0.0
    if (left <= right or right <= left) and len(common) >= 2:
        return 0.9
    return len(common) / len(left | right)


class FundMatcher:
    def __init__(self, funds: Iterable[Fund], brand_words: Iterable[str] = ()) -> None:
        self.funds = list(funds)
        self.by_rule = {f.rule_number: f for f in self.funds}
        self._brand = frozenset(w.lower() for w in brand_words)
        self._candidates: list[_Candidate] = []
        for fund in self.funds:
            for name in (fund.name, fund.short_name, *fund.previous_names):
                self._candidates.append(_Candidate(fund, self._clean(name_tokens(name))))

    def _clean(self, tokens: frozenset[str]) -> frozenset[str]:
        trimmed = tokens - self._brand
        return trimmed or tokens

    def by_text_rule(self, text: str) -> Fund | None:
        hits = {self.by_rule[r] .rule_number for r in rule_numbers_in(text) if r in self.by_rule}
        return self.by_rule[hits.pop()] if len(hits) == 1 else None

    def match(self, text: str, *, fund_type: FundType | None = None, threshold: float = 0.72) -> Fund | None:
        if _CLOSED_FUND.search(text):
            return None  # Закрытых фондов в реестре проекта нет, поэтому близкое имя было бы ошибкой.
        hint = fund_type or fund_type_hint(text)
        wanted = self._clean(name_tokens(text))
        scores: dict[str, float] = {}
        for candidate in self._candidates:
            if hint and candidate.fund.fund_type != hint:
                continue
            score = _score(wanted, candidate.tokens)
            scores[candidate.fund.rule_number] = max(score, scores.get(candidate.fund.rule_number, 0.0))
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        if not ranked or ranked[0][1] < threshold:
            return None
        if len(ranked) > 1 and ranked[1][1] >= ranked[0][1] - 0.02:
            return None
        return self.by_rule[ranked[0][0]]
