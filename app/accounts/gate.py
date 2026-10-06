"""A cap on how many password hashes run at the same time (spec 13.1 [P]).

Each argon2 hash uses about 19 MiB, and the routes run in worker threads (about 40 by default).
On a small free host a burst of logins could use more memory than the host has. At most `limit`
hashes run at once (default 2, PASSWORD_HASH_CONCURRENCY); a request that arrives when all slots
are taken is not queued: it is refused with a 503 and a short Retry-After, and costs no memory.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

DEFAULT_LIMIT = 2
MAX_LIMIT = 16


class HashBusy(Exception):
    """Every hashing slot is in use."""


class HashGate:
    def __init__(self, limit: int = DEFAULT_LIMIT) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"the hashing limit must be a whole number from 1 to {MAX_LIMIT}")
        self.limit = limit
        self._slots = threading.BoundedSemaphore(limit)

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Take a slot for the length of the block, or raise HashBusy at once."""
        if not self._slots.acquire(blocking=False):
            raise HashBusy
        try:
            yield
        finally:
            self._slots.release()
