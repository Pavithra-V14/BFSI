"""One turn at a time per session (the Phase 7 rule for Phases 8 and 10).

Two questions in one session at the same moment (two browser tabs) would get different message
numbers but their rows could interleave. `SessionLocks.try_acquire` refuses a second turn while one
is being answered; the caller answers "another message is still being answered". The lock is held in
this process (one Render instance); more than one instance would need a shared lock later.

The release is the caller's `finally` (see `Pipeline.answer`), so it happens on success, on a
timeout, on cancellation and on any error. A session id is only ever held once.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class SessionLocks:
    def __init__(self) -> None:
        self._held: set[tuple[str, str]] = set()
        self._mutex = threading.Lock()

    def try_acquire(self, user_id: str, session_id: str) -> bool:
        key = (user_id, session_id)
        with self._mutex:
            if key in self._held:
                return False
            self._held.add(key)
            return True

    def release(self, user_id: str, session_id: str) -> None:
        with self._mutex:
            self._held.discard((user_id, session_id))

    def held(self) -> int:
        with self._mutex:
            return len(self._held)


class WriteGuard:
    """Tracks the transcript write of ONE request that runs in a worker thread.

    A timed-out write keeps running in its thread. The session must stay locked until it ends, or
    the next turn of the same session could interleave with it. `release_when_idle` calls the
    release at once when no write runs, otherwise when the last tracked write ends (from the
    worker thread).
    """

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._running = 0
        self._then: Callable[[], None] | None = None

    def track(self, future: Any) -> None:
        """Register a `concurrent.futures.Future`. Its completion, or its cancellation before it
        started, ends the write."""
        with self._mutex:
            self._running += 1
        future.add_done_callback(self._ended)

    def _ended(self, _future: Any) -> None:
        then = None
        with self._mutex:
            self._running -= 1
            if self._running == 0 and self._then is not None:
                then, self._then = self._then, None
        if then is not None:
            then()

    def release_when_idle(self, release: Callable[[], None]) -> None:
        with self._mutex:
            if self._running > 0:
                self._then = release
                return
        release()

    @property
    def running(self) -> int:
        with self._mutex:
            return self._running
