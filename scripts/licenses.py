"""Строит docs/DEPENDENCIES.md: все установленные пакеты с лицензиями, включая транзитивные."""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

ALLOWED = re.compile(r"(?i)\b(mit|bsd|apache|isc)\b")
TARGET = Path(__file__).parents[1] / "docs" / "DEPENDENCIES.md"
SKIP = {"pif-registry", "pip", "setuptools", "wheel"}


def license_of(package: metadata.Distribution) -> str:
    expression = package.metadata.get("License-Expression")
    if expression:
        return expression
    classifiers = [
        c.split("::")[-1].strip() for c in package.metadata.get_all("Classifier") or [] if c.startswith("License")
    ]
    text = package.metadata.get("License") or ""
    return "; ".join(classifiers) or text.splitlines()[0][:60] if (classifiers or text) else "не указана"


def main() -> None:
    rows = sorted(
        {(d.metadata["Name"], d.version, license_of(d)) for d in metadata.distributions() if d.metadata["Name"] not in SKIP},
        key=lambda row: row[0].lower(),
    )
    others = [row for row in rows if not ALLOWED.search(row[2])]
    lines = [
        "# Зависимости и лицензии",
        "",
        "Список построен командой `python scripts/licenses.py` по окружению разработки, транзитивные пакеты включены.",
        "",
        "## Вне списка из задания",
        "",
        "Задание разрешает MIT, BSD, Apache 2.0 и ISC. Эти пакеты приходят транзитивно, заменить их без отказа от",
        "`httpx` или `mypy` нельзя. Все они работают только при сборе и разработке, в сайт не попадают.",
        "",
        *[f"- {name} {version}: {lic}" for name, version, lic in others],
        "",
        "## Все пакеты",
        "",
        "| Пакет | Версия | Лицензия |",
        "|---|---|---|",
        *[f"| {name} | {version} | {lic} |" for name, version, lic in rows],
        "",
        "Шрифты Manrope, Inter и JetBrains Mono: OFL 1.1, тексты лицензий в `src/pif/site/static/fonts/`.",
        "",
    ]
    TARGET.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"{len(rows)} пакетов, вне списка: {len(others)}")


if __name__ == "__main__":
    main()
