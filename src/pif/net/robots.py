"""Разбор robots.txt с поддержкой шаблонов `*` и `$`.

Стандартный urllib.robotparser понимает только префиксы и пропустил бы правила вроде
`Disallow: /*?*`, которые на сайтах УК встречаются часто. Выигрывает самое длинное совпадение,
при равной длине Allow важнее Disallow (RFC 9309).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class _Rule:
    allow: bool
    length: int
    regex: re.Pattern[str]


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(f"^{regex}{'$' if anchored else ''}")


@dataclass(slots=True)
class RobotsRules:
    rules: list[_Rule] = field(default_factory=list)
    crawl_delay: float | None = None

    @classmethod
    def parse(cls, text: str, agent_token: str) -> RobotsRules:
        agent_token = agent_token.lower()
        groups: list[tuple[set[str], list[tuple[str, str]]]] = []
        agents: set[str] = set()
        lines: list[tuple[str, str]] = []
        collecting_agents = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            name, value = (part.strip() for part in line.split(":", 1))
            name = name.lower()
            if name == "user-agent":
                if not collecting_agents:
                    agents, lines = set(), []
                    groups.append((agents, lines))
                    collecting_agents = True
                agents.add(value.lower())
            elif groups:
                collecting_agents = False
                lines.append((name, value))

        chosen = next((g for g in groups if agent_token in g[0]), None)
        chosen = chosen or next((g for g in groups if "*" in g[0]), None)
        result = cls()
        for name, value in chosen[1] if chosen else []:
            if name in ("allow", "disallow") and value:
                result.rules.append(_Rule(name == "allow", len(value), _compile(value)))
            elif name == "crawl-delay":
                try:
                    result.crawl_delay = float(value)
                except ValueError:
                    continue
        return result

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"
        best: _Rule | None = None
        for rule in self.rules:
            if rule.regex.match(target) and (
                best is None
                or rule.length > best.length
                or (rule.length == best.length and rule.allow and not best.allow)
            ):
                best = rule
        return best is None or best.allow
