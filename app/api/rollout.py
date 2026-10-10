"""The rollout stage (spec 19: shadow mode, then a staff beta, then customers) [P].

One setting, `rollout_stage`, in the database (`app_settings`), with three values:

- `shadow`: only admins see answers;
- `staff_beta`: admins and staff see answers;
- `open`: everyone sees answers.

A user who may not see answers still has their turn generated and stored for evaluation (the
assistant message is flagged `shadowed` for good), and gets a fixed notice with no answer text,
citations or scores. The stage is cached for a few seconds. If it cannot be read, or holds a value
that is not one of the three, it is `shadow` (fail closed). A missing row is `shadow` too.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.accounts.audit import write_audit
from app.accounts.tokens import utc_text

log = logging.getLogger("app.api")

SETTING_KEY = "rollout_stage"
STAGES = ("shadow", "staff_beta", "open")
DEFAULT_STAGE = "shadow"
CACHE_SECONDS = 5.0

SHADOW_NOTICE = (
    "The assistant is in a test period. Your question was received, but answers are not shown yet."
)
WITHDRAWN_NOTICE = "This answer cited a document that has since been withdrawn."


def answers_visible(stage: str, role: str) -> bool:
    """May a user with this role see answers at this stage? Unknown stage or role: no."""
    if role == "admin":
        return True
    if stage == "open":
        return role in ("customer", "staff")
    if stage == "staff_beta":
        return role == "staff"
    return False


class Rollout:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        cache_seconds: float = CACHE_SECONDS,
    ) -> None:
        self._clock = clock
        self._on_event = on_event
        self._cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._value: str | None = None
        self._until = 0.0

    def _emit(self, name: str, **fields: Any) -> None:
        if self._on_event:
            try:
                self._on_event(name, fields)
            except Exception:  # an alert hook must never break a request
                pass

    def stage(self, db: Any) -> str:
        """The stage now (cached for a few seconds). Never raises: any trouble means `shadow`."""
        with self._lock:
            if self._value is not None and self._clock() < self._until:
                return self._value
        try:
            with db.transaction():
                row = db.query_one("SELECT value FROM app_settings WHERE key = ?", (SETTING_KEY,))
        except Exception as error:  # fail closed: nobody but an admin sees answers
            self._emit("rollout_stage_unreadable", reason=type(error).__name__)
            return DEFAULT_STAGE
        value = row[0] if row else DEFAULT_STAGE
        if value not in STAGES:
            self._emit("rollout_stage_invalid")
            value = DEFAULT_STAGE
        with self._lock:
            self._value, self._until = value, self._clock() + self._cache_seconds
        return value

    def set_stage(self, db: Any, *, stage: str, actor_id: str, now: datetime) -> str:
        """Change the stage (admin only: the route checks). Writes one audit row. Returns the
        previous stage."""
        if stage not in STAGES:
            raise ValueError("stage must be shadow, staff_beta or open")
        with db.transaction(write=True):
            row = db.query_one("SELECT value FROM app_settings WHERE key = ?", (SETTING_KEY,))
            previous = row[0] if row else DEFAULT_STAGE
            # one statement: the first change cannot collide with another first change
            db.execute(
                "INSERT INTO app_settings (key, value, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
                "value = excluded.value, updated_by = excluded.updated_by, "
                "updated_at = excluded.updated_at",
                (SETTING_KEY, stage, actor_id, utc_text(now)),
            )
            write_audit(
                db,
                actor=actor_id,
                action="rollout_stage_change",
                target=SETTING_KEY,
                details={"from": previous, "to": stage},
                now=now,
            )
        with self._lock:
            self._value, self._until = stage, self._clock() + self._cache_seconds
        return previous
