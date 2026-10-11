"""The observability parts, put together for the API (Phase 12).

`Observability` owns the live metrics, the alert manager, the trace-deletion worker and the rollup
writer, and starts and stops their background threads. The API builds one from the environment
(`from_env`) and gives it the database factory; tests build one with fakes and call `rollup_once`
and `process_traces_once` themselves (no thread needed).

Nothing here is on the request path except `observe` (a few list appends) and `queue_chat_traces`
(one small insert before a chat is deleted). A failure anywhere is an event with a class name.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from app.db.connection import Database
from app.observability import alerts as alerts_module
from app.observability import metrics, traces
from app.observability.webhook import sender_from_env
from app.pipeline import langfuse_sink

log = logging.getLogger("app.observability")


def day_floor(moment: datetime) -> datetime:
    return metrics.day_start(moment)


ROLLUP_SECONDS = 300.0
TRACE_SECONDS = 60.0
AUDIT_RETENTION_DAYS = 30  # the pilot value (spec 12.1); to be revisited in Phase 16
ROLLUP_HOURLY_DAYS = 30


class Observability:
    def __init__(
        self,
        *,
        db_factory: Callable[[], Database],
        alerts: alerts_module.AlertManager,
        hub: metrics.MetricsHub | None = None,
        deleter: traces.TraceDeleter | None = None,
        trace_key: str | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._db_factory = db_factory
        self.alerts = alerts
        self.hub = hub or metrics.MetricsHub(now=now)
        self.deleter = deleter
        self.trace_key = trace_key
        self._on_event = on_event
        self._now = now or (lambda: datetime.now(UTC))
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ---- the request side ----------------------------------------------------------
    def observe(self, ctx: Any, result: Any) -> None:
        """Called once per finished chat request. Never raises."""
        try:
            obs = metrics.observation_from(ctx, result, self._now().timestamp())
            self.hub.observe(obs)
            self.alerts.observe(obs)
        except Exception as error:
            self._event("observability_failed", {"reason": type(error).__name__})

    def observe_early(self, request: Any, result: Any) -> None:
        """A request that ended before the graph ran (service starting, masker or setup failed).
        It counts as an unavailable answer in the metrics and the error-rate window.
        Never raises."""
        try:
            obs = metrics.RequestObservation(
                at=self._now().timestamp(),
                role=str(getattr(request, "role", "")),
                kind="unavailable",
                reason=str(result.reason) if getattr(result, "reason", None) else None,
            )
            self.hub.observe(obs)
            self.alerts.observe(obs)
        except Exception as error:
            self._event("observability_failed", {"reason": type(error).__name__})

    def queue_chat_traces(self, db: Database, session_ids: list[str]) -> None:
        """Remember that these chats' traces must go. Called BEFORE the chats are deleted."""
        if self.trace_key is None or not session_ids:
            return
        hashes = [traces.session_hash(self.trace_key, s) for s in session_ids]
        traces.enqueue(db, hashes, self._now())

    def _event(self, name: str, fields: dict[str, Any]) -> None:
        if self._on_event is not None:
            try:
                self._on_event(name, fields)
            except Exception:
                pass

    # ---- the background side -------------------------------------------------------
    def rollup_once(self) -> int:
        """Save the increments waiting in memory and refresh the daily counts of today and
        yesterday from the tables. Returns the number of rows written."""
        written = 0
        db = self._db_factory()
        try:
            pending = self.hub.take_pending()
            if pending:
                try:
                    written += metrics.add_hourly(db, pending)
                except Exception:
                    # not saved: put them back so the next pass tries again
                    self.hub.restore(pending)
                    raise
            today = self._now()
            for day in (today - timedelta(days=1), today):
                written += metrics.set_daily(db, day, metrics.daily_from_tables(db, day))
        finally:
            db.close()
        return written

    def process_traces_once(self) -> traces.PassResult | None:
        if self.deleter is None:
            return None
        db = self._db_factory()
        try:
            return traces.process_pending(db, self.deleter, self._now(), on_event=self._forward)
        finally:
            db.close()

    def _forward(self, name: str, fields: dict[str, Any]) -> None:
        self.alerts.on_event(name, fields)
        self._event(name, fields)

    def seed_tokens(self) -> None:
        """Today's tokens already saved (hourly rows) become the start of the day's total."""
        db = self._db_factory()
        try:
            today = day_floor(self._now())
            totals = metrics.hourly_totals(db, today, today + timedelta(days=1))
            used = totals.get(("all", "tokens_in"), 0.0) + totals.get(("all", "tokens_out"), 0.0)
            self.hub.seed_tokens(today, int(used))
        finally:
            db.close()

    def start(self) -> None:
        if self._threads:
            return
        try:
            self.seed_tokens()
        except Exception as error:  # a down database must not stop the service from starting
            self._event("observability_failed", {"reason": type(error).__name__})
        self.alerts.start()
        for target, seconds, name in (
            (self.rollup_once, ROLLUP_SECONDS, "rollup-writer"),
            (self.process_traces_once, TRACE_SECONDS, "trace-deleter"),
        ):
            thread = threading.Thread(
                target=self._every, args=(target, seconds), name=name, daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def _every(self, work: Callable[[], Any], seconds: float) -> None:
        while not self._stop.wait(seconds):
            try:
                work()
            except Exception as error:
                self._event("observability_failed", {"reason": type(error).__name__})

    def flush_pending(self) -> int:
        """Save the increments waiting in memory; touches the database only when there are some."""
        pending = self.hub.take_pending()
        if not pending:
            return 0
        db = self._db_factory()
        try:
            return metrics.add_hourly(db, pending)
        except Exception:
            self.hub.restore(pending)
            raise
        finally:
            db.close()

    def stop(self) -> None:
        self._stop.set()
        self.alerts.stop()
        try:
            self.flush_pending()  # the last increments
        except Exception:
            pass


def from_env(
    env: Mapping[str, str],
    db_factory: Callable[[], Database],
    *,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> Observability:
    """Build the parts from the shell's settings. A webhook or threshold setting that cannot be
    used raises `ValueError` / `WebhookConfigError` naming the variable (never a value)."""
    config = alerts_module.config_from_env(env)
    sender = sender_from_env(env)
    store = alerts_module.AlertStore(db_factory)
    hub = metrics.MetricsHub()
    manager = alerts_module.AlertManager(
        store=store,
        sender=sender,
        config=config,
        on_event=on_event,
        tokens_today=hub.tokens_today,
    )
    deleter = None
    trace_key = None
    try:
        langfuse = langfuse_sink.config_from_env(env)
    except langfuse_sink.SinkConfigError:
        langfuse = None  # the sink itself refuses to start with it; nothing to delete from
    if langfuse is not None:
        deleter = traces.TraceDeleter.from_config(langfuse)
        trace_key = langfuse.user_hash_key
    return Observability(
        db_factory=db_factory,
        alerts=manager,
        hub=hub,
        deleter=deleter,
        trace_key=trace_key,
        on_event=on_event,
    )
