"""Контентно-адресуемое хранилище сырых ответов: raw/<sha[:2]>/<sha>.<ext>.

Одинаковое содержимое лежит один раз, поэтому повторный запуск не плодит файлы.
Каталог не попадает в git: его содержимое восстанавливается по манифесту.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

SOURCE_ID_LEN = 16


def sha256_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def source_id_of(sha256: str) -> str:
    return sha256[:SOURCE_ID_LEN]


class RawStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, sha256: str, ext: str) -> Path:
        return self.root / sha256[:2] / f"{sha256}.{ext}"

    def put(self, content: bytes, ext: str) -> tuple[str, Path]:
        sha = sha256_of(content)
        path = self.path_for(sha, ext)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(content)
            tmp.replace(path)
        return sha, path

    def get(self, sha256: str, ext: str) -> bytes | None:
        path = self.path_for(sha256, ext)
        return path.read_bytes() if path.exists() else None
