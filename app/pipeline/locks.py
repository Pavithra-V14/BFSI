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
