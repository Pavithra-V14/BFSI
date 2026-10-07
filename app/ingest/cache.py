"""A small local cache for the paid-quota calls of ingestion (captions, table summaries, labels).

A document with a failed page is not activated, so the next run would otherwise repeat every
caption and label call for the pages that worked. The cache keys each result by the SHA-256 of
what was sent, so a changed input is never answered from the cache. It lives under the
git-ignored `originals/` folder, holds only text derived from the document, and can be deleted at
any time (the next run simply asks again).
"""

from __future__ import annotations

import hashlib
from pathlib import Path


class DiskCache:
    def __init__(self, directory: Path | None) -> None:
        self.directory = directory

    def _path(self, kind: str, text: str) -> Path | None:
        if self.directory is None:
            return None
        digest = hashlib.sha256(f"{kind}\x00{text}".encode()).hexdigest()
        return self.directory / kind / digest[:2] / f"{digest}.txt"

    def get(self, kind: str, text: str) -> str | None:
        path = self._path(kind, text)
        if path is None or not path.is_file():
            return None
        try:
            return path.read_text("utf-8")
        except OSError:
            return None

    def put(self, kind: str, text: str, value: str) -> None:
        path = self._path(kind, text)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, "utf-8")
        except OSError:
            pass  # a cache that cannot write is just slower, never wrong
