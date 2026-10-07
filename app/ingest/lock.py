"""A per-document lock (spec 6.3): ingest, reindex and delete never run together on one document.

An operating-system file lock, so it is released by the system if the program dies (there is no
stale lock to clean up). It protects against two terminals on the same machine, which is how
ingestion is run (admin machine only).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.ingest.model import IngestError, check_doc_id


class DocumentBusy(IngestError):
    """Another ingest, reindex or delete is running on this document."""


def _lock(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def document_lock(directory: Path, doc_id: str) -> Iterator[None]:
    check_doc_id(doc_id)
    directory.mkdir(parents=True, exist_ok=True)
    handle = open(directory / f"{doc_id}.lock", "a+b")  # noqa: SIM115 (closed in finally)
    try:
        try:
            _lock(handle)
        except OSError:
            raise DocumentBusy(
                f"another ingest, reindex or delete is running on '{doc_id}'; "
                "try again when it ends"
            ) from None
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()
