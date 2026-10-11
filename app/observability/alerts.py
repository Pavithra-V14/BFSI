"""Alerts (spec 14, 18): rules, persistence, the banner and the chat webhook.

Two sources of an alert:
- an EVENT the system already emits (all keys exhausted, the masker failed to load, a trace
  deletion kept failing ...). `EVENT_SEVERITY` says which events are alerts and how serious;
- a RULE over a window of finished requests (error rate, fallback rate, guardrail spike) or over
  the day (budget variance). A rule needs `min_requests` requests in its window before it can fire,
  so one failed request among three is not "a rate".

Severity: `critical` goes to the banner AND the webhook; `warning` goes to the banner only.

Limits that keep it quiet: ONE alert per name per hour (later events in that hour only raise the
count of the row already written), and a daily cap on webhook messages (a count of today's
`notified` rows, so it survives a restart).

What is stored and sent: the alert's name, severity, a count, the window and the time. Never a
user, a session, a text or an address; an exception appears as its class name only.

Writing and sending happen on a background thread through a bounded queue, never on the request
path; a failure of the database or the webhook is an event (class name), never an error for a user.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.accounts.tokens import utc_text
from app.db.connection import Database
from app.observability.metrics import RequestObservation
from app.observability.webhook import WebhookSender, failure_event, message_for

CRITICAL, WARNING = "critical", "warning"
QUEUE_SIZE = 500
FAILURE_EVENT_SECONDS = 60.0  # a failing channel reports itself at most once a minute
BANNER_HOURS = 24

# Events that are alerts. Every other event stays in the in-process buffer only.
EVENT_SEVERITY: dict[str, str] = {
    "all_keys_exhausted": CRITICAL,
    "embedding_unavailable": CRITICAL,
    "masker_load_failed": CRITICAL,
    "masker_unavailable": CRITICAL,
    "cache_quota_exceeded": CRITICAL,
    "cache_out_of_memory": CRITICAL,
    "cache_read_only": CRITICAL,
    "vector_store_rejected": CRITICAL,
    "budget_charge_failed": CRITICAL,
    "graph_failed": CRITICAL,
    "sparse_version_mismatch": CRITICAL,
    "persist_queue_overflow": CRITICAL,
    "persist_job_dead_lettered": CRITICAL,
    "trace_deletion_failed": CRITICAL,
    "trace_queue_failed": CRITICAL,  # a chat was deleted but its traces could not be queued
    "trace_deletion_cap_reached": CRITICAL,  # a chat has more traces than one pass removes
    "jev_unavailable": WARNING,
    "decision_fell_to_rules": WARNING,
    "rerank_failed": WARNING,
    "langfuse_failed": WARNING,
    "alert_webhook_failed": WARNING,
}

# Outcomes that count as errors in the error-rate window ("busy" is the user's own doing).
ERROR_KINDS = frozenset({"unavailable", "starting_up"})

# Result reasons that are the guardrails saying no (not "out of domain" or "no context").
GUARDRAIL_REASONS = frozenset(
    {"blocked", "injection", "too_long", "too_short", "empty", "unknown_role", "reword"}
)


@dataclass(frozen=True)
class AlertConfig:
    """The thresholds are [T] placeholders to tune after the baseline run (spec 15, Phase 16)."""

    window_seconds: float = 900.0
    min_requests: int = 20
    error_rate: float = 0.25  # [T] share of requests that ended "unavailable"
    fallback_rate: float = 0.30  # [T] share of requests that ran in a reduced mode
    guardrail_rate: float = 0.30  # [T] share of requests the guardrails refused
    daily_token_plan: int = 340_000  # [T] 100 queries a day at about 3,400 tokens (spec 18)
    budget_variance: float = 1.20  # [T] fire when today's tokens pass this multiple of the plan
    webhook_daily_cap: int = 20  # [T] webhook messages per UTC day
    per_name_seconds: float = 3_600.0  # one alert per name per hour


MIN_REQUESTS_FLOOR = 20  # a rule never fires on fewer requests than this (spec: [T] 20)


def config_from_env(env: Mapping[str, str]) -> AlertConfig:
    """Overrides by name. Every value is checked: a number that is finite and inside its range.
    A bad one raises `ValueError` naming the variable (never its value), so the service does not
    start with an alert that can never fire (a rate of 25, "nan") or one that fires on noise."""
    import math

    base = AlertConfig()
    # variable: (field, low, high, whole number?, factor to the field's unit)
    rules: dict[str, tuple[str, float, float, bool, float]] = {
        "ALERT_WINDOW_MINUTES": ("window_seconds", 1.0, 1440.0, False, 60.0),
        "ALERT_MIN_REQUESTS": ("min_requests", float(MIN_REQUESTS_FLOOR), 100_000.0, True, 1.0),
        "ALERT_ERROR_RATE": ("error_rate", 0.0001, 1.0, False, 1.0),
        "ALERT_FALLBACK_RATE": ("fallback_rate", 0.0001, 1.0, False, 1.0),
        "ALERT_GUARDRAIL_RATE": ("guardrail_rate", 0.0001, 1.0, False, 1.0),
        "ALERT_DAILY_TOKEN_PLAN": ("daily_token_plan", 1.0, 1e9, True, 1.0),
        "ALERT_BUDGET_VARIANCE": ("budget_variance", 1.0, 100.0, False, 1.0),
        "ALERT_WEBHOOK_DAILY_CAP": ("webhook_daily_cap", 1.0, 1000.0, True, 1.0),
    }
    values: dict[str, Any] = {}
    for variable, (field_name, low, high, whole, factor) in rules.items():
        raw = (env.get(variable) or "").strip()
        if not raw:
            continue
        try:
            number = float(raw)
        except ValueError:
            raise ValueError(f"{variable} must be a number") from None
        if not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f"{variable} must be a number from {low:g} to {high:g}")
        if whole and number != int(number):
            raise ValueError(f"{variable} must be a whole number")
        values[field_name] = int(number) if whole else number * factor
    return AlertConfig(**{**base.__dict__, **values})


# ---- the table -------------------------------------------------------------------------------
class AlertStore:
    """Reads and writes `alert_events`. Each call opens its own connection and closes it."""

    def __init__(self, db_factory: Callable[[], Database]) -> None:
        self._factory = db_factory

    def _run(self, work: Callable[[Database], Any]) -> Any:
        db = self._factory()
        try:
            return work(db)
        finally:
            db.close()

    def insert(self, name: str, severity: str, count: int, notified: bool, now: datetime) -> str:
        row_id = str(uuid.uuid4())

        def work(db: Database) -> None:
            with db.transaction(write=True):
                db.execute(
                    "INSERT INTO alert_events (id, name, severity, event_count, notified, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (row_id, name, severity, int(count), bool(notified), utc_text(now)),
                )

        self._run(work)
        return row_id

    def bump(self, row_id: str, extra: int) -> None:
        def work(db: Database) -> None:
            with db.transaction(write=True):
                db.execute(
                    "UPDATE alert_events SET event_count = event_count + ? WHERE id = ?",
                    (int(extra), row_id),
                )

        self._run(work)

    def mark_notified(self, row_id: str) -> None:
        def work(db: Database) -> None:
            with db.transaction(write=True):
                db.execute("UPDATE alert_events SET notified = ? WHERE id = ?", (True, row_id))

        self._run(work)

    def recent(self, since: datetime, limit: int = 100) -> list[dict[str, Any]]:
        def work(db: Database) -> list[dict[str, Any]]:
            with db.transaction():
                rows = db.query_all(
                    "SELECT name, severity, event_count, notified, created_at FROM alert_events "
                    "WHERE created_at >= ? ORDER BY created_at DESC, id LIMIT ?",
                    (utc_text(since), int(limit)),
                )
            return [
                {
                    "name": r[0],
                    "severity": r[1],
                    "count": int(r[2]),
                    "notified": bool(r[3]),
                    "at": str(r[4]),
                }
                for r in rows
            ]

        return self._run(work)

    def notified_since(self, since: datetime) -> int:
        def work(db: Database) -> int:
            with db.transaction():
                row = db.query_one(
                    "SELECT COUNT(*) FROM alert_events WHERE notified = ? AND created_at >= ?",
                    (True, utc_text(since)),
                )
            return int(row[0]) if row else 0

        return self._run(work)

    def last_in_hour(self, name: str, since: datetime) -> tuple[str, datetime] | None:
        def work(db: Database) -> tuple[str, datetime] | None:
            with db.transaction():
                row = db.query_one(
                    "SELECT id, created_at FROM alert_events WHERE name = ? AND created_at >= ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (name, utc_text(since)),
                )
            if row is None:
                return None
            stamp = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
            return str(row[0]), stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)

        return self._run(work)


# ---- the manager -----------------------------------------------------------------------------
class AlertManager:
    def __init__(
        self,
        *,
        store: AlertStore | None,
        sender: WebhookSender | None = None,
        config: AlertConfig | None = None,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        background: bool = True,
        tokens_today: Callable[[], int] | None = None,
    ) -> None:
        self.config = config or AlertConfig()
        self._store = store
        self._sender = sender
        self._now = now or (lambda: datetime.now(UTC))
        self._clock = clock
        self._on_event = on_event
        self._tokens_today = tokens_today
        self._lock = threading.Lock()
        self._recent: dict[str, tuple[str | None, datetime]] = {}  # name -> (row id, time)
        self._window: deque[RequestObservation] = deque(maxlen=20_000)
        self._last_failure_event = -1e9
        self._queue: queue.Queue[tuple[str, str, int, int | None, bool]] = queue.Queue(QUEUE_SIZE)
        self._last_count: dict[str, int] = {}  # a rule's count at the last write
        self.dropped = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._background = background

    # ---- lifecycle -----------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None or not self._background:
            return
        self._thread = threading.Thread(target=self._loop, name="alert-writer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._process(*item)

    def flush(self) -> None:
        """Process everything queued now (tests, and a clean shutdown)."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            self._process(*item)

    # ---- feeding -------------------------------------------------------------------
    def on_event(self, name: str, fields: Mapping[str, Any] | None = None) -> None:
        """An event of the system. Only the events in `EVENT_SEVERITY` become alerts."""
        severity = EVENT_SEVERITY.get(name)
        if severity is None:
            return
        self._submit(name, severity, 1, None)

    def observe(self, obs: RequestObservation) -> None:
        """A finished request: feeds the window rules and the budget-variance rule."""
        with self._lock:
            self._window.append(obs)
            cutoff = obs.at - self.config.window_seconds
            while self._window and self._window[0].at < cutoff:
                self._window.popleft()
            items = list(self._window)
        self._rate_rules(items)
        self._budget_rule()

    def _rate_rules(self, items: list[RequestObservation]) -> None:
        cfg = self.config
        total = len(items)
        if total < cfg.min_requests:  # a rule needs enough requests before it can fire
            return
        minutes = int(cfg.window_seconds // 60)
        rules = (
            (
                "error_rate",
                CRITICAL,
                sum(1 for o in items if o.kind in ERROR_KINDS),
                cfg.error_rate,
            ),
            ("fallback_rate", WARNING, sum(1 for o in items if o.degraded), cfg.fallback_rate),
            (
                "guardrail_spike",
                WARNING,
                sum(1 for o in items if o.kind == "refusal" and o.reason in GUARDRAIL_REASONS),
                cfg.guardrail_rate,
            ),
        )
        for name, severity, count, threshold in rules:
            if count / total >= threshold:
                self._submit(name, severity, count, minutes, cumulative=True)

    def _budget_rule(self) -> None:
        if self._tokens_today is None:
            return
        try:
            used = int(self._tokens_today())
        except Exception:
            return
        limit = self.config.daily_token_plan * self.config.budget_variance
        if used >= limit:
            self._submit("budget_variance", WARNING, 1, None)

    # ---- firing --------------------------------------------------------------------
    def _submit(
        self,
        name: str,
        severity: str,
        count: int,
        window: int | None,
        cumulative: bool = False,
    ) -> None:
        """`cumulative`: `count` is the size of the window (a rule), not one new occurrence."""
        try:
            self._queue.put_nowait((name, severity, count, window, cumulative))
        except queue.Full:
            self.dropped += 1
            return
        if not self._background:
            self.flush()

    def _process(
        self, name: str, severity: str, count: int, window: int | None, cumulative: bool = False
    ) -> None:
        now = self._now()
        try:
            self._fire(name, severity, count, window, now, cumulative)
        except Exception as error:  # the database or the channel: an event, never an exception
            self._report_failure(error)

    def _fire(
        self,
        name: str,
        severity: str,
        count: int,
        window: int | None,
        now: datetime,
        cumulative: bool = False,
    ) -> None:
        cfg = self.config
        with self._lock:
            known = self._recent.get(name)
        if known is None and self._store is not None:
            found = self._store.last_in_hour(name, now - timedelta(seconds=cfg.per_name_seconds))
            if found is not None:
                known = (found[0], found[1])
        if known is not None and (now - known[1]).total_seconds() < cfg.per_name_seconds:
            # one alert per name per hour: the rows already written only get a bigger count. A
            # rule's count is the size of its window, so only the rise since the last write is
            # added (a rule that stays above its threshold does not add the whole window again
            # on every request); an event is one occurrence, so it adds its own count.
            with self._lock:
                last = self._last_count.get(name)
                # after a restart the row is known but not its last count: add nothing, then track
                delta = (
                    max(0, count - (last if last is not None else count)) if cumulative else count
                )
                if cumulative:
                    self._last_count[name] = max(count, last or 0)
                self._recent[name] = known
            if delta > 0 and self._store is not None and known[0] is not None:
                self._store.bump(known[0], delta)
            return
        notify = severity == CRITICAL and self._sender is not None
        if notify and self._store is not None:
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if self._store.notified_since(day) >= cfg.webhook_daily_cap:
                notify = False  # the daily cap on webhook messages
        row_id = None
        if self._store is not None:
            row_id = self._store.insert(name, severity, count, False, now)
        with self._lock:
            self._recent[name] = (row_id, now)
            if cumulative:
                self._last_count[name] = count
        if notify and self._sender is not None:
            try:
                self._sender.send(message_for(name, severity, count, window))
            except Exception as error:  # the row stays "not notified"; the failure is an event
                self._report_failure(error)
                return
            if row_id is not None and self._store is not None:
                self._store.mark_notified(row_id)

    def _report_failure(self, error: BaseException) -> None:
        """A failing database or webhook is reported, by class name, at most once a minute."""
        moment = self._clock()
        if moment - self._last_failure_event < FAILURE_EVENT_SECONDS:
            return
        self._last_failure_event = moment
        if self._on_event is not None:
            name, fields = failure_event(error)
            try:
                self._on_event(name, fields)
            except Exception:
                pass

    # ---- reading -------------------------------------------------------------------
    def recent(self, hours: int = BANNER_HOURS) -> list[dict[str, Any]]:
        if self._store is None:
            return []
        try:
            return self._store.recent(self._now() - timedelta(hours=hours))
        except Exception:
            return []
