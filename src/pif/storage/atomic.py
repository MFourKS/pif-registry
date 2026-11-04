"""Атомарная замена файла. На Windows её ненадолго блокируют антивирус и читающие процессы."""

from __future__ import annotations

import time
from pathlib import Path

ATTEMPTS = 8
PAUSE_S = 0.25


def replace_file(tmp: Path, target: Path) -> None:
    for attempt in range(ATTEMPTS):
        try:
            tmp.replace(target)
            return
        except PermissionError:
            if attempt + 1 == ATTEMPTS:
                raise
            time.sleep(PAUSE_S)
