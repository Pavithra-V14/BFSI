"""The pending-finish marker (spec 6.3 [P], 10.3 item 6).

Ingest, review and delete are not complete until the corpus version has been bumped and the cache
entries cleared. The marker is a tiny file per document, `<marker folder>/<doc_id>.pending`, that is
set BEFORE the vector writes and removed only when the bump and every invalidation succeeded.

- A run that finds the marker, even when nothing in the document changed, redoes the bump and the
  document-wide invalidation (that also covers every chunk), so a repeat really finishes the job.
- A run that finds no marker and no change is a true no-op: no write, no bump, no audit row.

The file holds the document id, the time it was first set and the kind of run. No document text.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from app.accounts.tokens import utc_text
from app.cache.store import CacheStore
from app.ingest.model import check_doc_id


def marker_path(directory: Path, doc_id: str) -> Path:
    return directory / f"{check_doc_id(doc_id)}.pending"


def is_pending(directory: Path, doc_id: str) -> bool:
    return marker_path(directory, doc_id).is_file()


def mark(directory: Path, doc_id: str, now: datetime, reason: str) -> None:
    """Set the marker. An existing marker keeps its first time."""
    path = marker_path(directory, doc_id)
    if path.is_file():
        return
    directory.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"doc_id": doc_id, "since": utc_text(now), "reason": reason}), "utf-8"
    )
    os.replace(temporary, path)


def clear(directory: Path, doc_id: str) -> None:
    marker_path(directory, doc_id).unlink(missing_ok=True)


def finish_cache_steps(
    cache: CacheStore,
    directory: Path,
    doc_id: str,
    *,
    chunk_ids: list[str],
    whole_document: bool,
    now: datetime,
    reason: str,
) -> list[str]:
    """Bump the corpus version, then clear the entries that cited the given chunks and (when
    `whole_document`) every entry that cited the document. Returns what failed (empty means done).
    The marker is set first and cleared only when nothing failed."""
    mark(directory, doc_id, now, reason)
    incomplete: list[str] = []
    if cache.bump_corpus_version() is None:
        incomplete.append("the corpus version was not bumped")
    failed = sum(cache.invalidate_chunk(chunk_id) is None for chunk_id in chunk_ids)
    if failed:
        incomplete.append(f"{failed} chunk cache invalidation(s) failed")
    if whole_document and cache.invalidate_doc(doc_id) is None:
        incomplete.append("the document cache invalidation failed")
    if not incomplete:
        clear(directory, doc_id)
    return incomplete
