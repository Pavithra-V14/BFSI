"""The numbers behind the dashboards (spec 5.3, 5.4, 15.6, 18).

`RequestObservation` is what one finished chat request leaves behind: NUMBERS and fixed words (the
result kind and reason code, which deciders answered which step, token counts, billed and shadow
cost). It holds no text, no user id and no session id. `MetricsHub` keeps the last day of them in
memory (live panels) and collects increments for the hourly rollups saved in `metric_rollups`.

Rollup rows are ADDED TO (`value = value + increment`), so a restart loses at most the last few
minutes and never counts anything twice. Daily database counts (active users, chats, ...) are
computed from the tables and REPLACED each time, because they are not additive.
"""

from __future__ import annotations

import math
import threading
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.accounts.tokens import utc_text
from app.db.connection import Database

HOUR = 3_600.0
DAY = 86_400.0
KEEP = 50_000  # most observations kept in memory
ROLES = ("customer", "staff")

# Rolled up per hour and role (additive).
HOURLY_METRICS = (
    "requests", "answered", "refused", "unavailable", "cached", "degraded", "low_confidence",
    "tokens_in", "tokens_out", "billed_cost", "shadow_cost",
)  # fmt: skip
# Computed from the tables per UTC day and role (replaced each time).
DAILY_DB_METRICS = (
    "active_users", "chats_started", "questions", "answers_verified", "fixed_replies",
    "thumbs_up", "thumbs_down", "seat_utilisation",
)  # fmt: skip


@dataclass(frozen=True)
class RequestObservation:
    at: float  # seconds since the epoch
    role: str  # customer | staff | admin
    kind: str  # the result kind: answer, refusal, unavailable, ...
    reason: str | None = None  # a fixed reason code
    cached: bool = False
    degraded: tuple[str, ...] = ()
    low_confidence: bool = False
    warnings: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    billed: float = 0.0
    shadow: float = 0.0
    stage_tokens: tuple[tuple[str, int], ...] = ()
    deciders: tuple[tuple[str, str], ...] = ()  # (step, decider)

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


def observation_from(ctx: Any, result: Any, at: float) -> RequestObservation:
    """Read the fixed fields of a finished request (the pipeline state and its result)."""
    spans = list(ctx.trace.spans)
    stage_tokens: dict[str, int] = {}
    deciders: list[tuple[str, str]] = []
    for span in spans:
        used = int(span.input_tokens or 0) + int(span.output_tokens or 0)
        if used:
            stage_tokens[span.stage] = stage_tokens.get(span.stage, 0) + used
        if span.decider:
            deciders.append((span.stage, str(span.decider)))
    return RequestObservation(
        at=at,
        role=str(ctx.request.role),
        kind=str(result.kind),
        reason=str(result.reason) if result.reason else None,
        cached=bool(result.cached),
        degraded=tuple(str(d) for d in result.degraded),
        low_confidence=result.confidence == "low",
        warnings=len(result.warnings),
        tokens_in=sum(int(s.input_tokens or 0) for s in spans),
        tokens_out=sum(int(s.output_tokens or 0) for s in spans),
        billed=sum(float(s.cost_billed or 0.0) for s in spans),
        shadow=sum(float(s.shadow_cost or 0.0) for s in spans),
        stage_tokens=tuple(sorted(stage_tokens.items())),
        deciders=tuple(deciders),
    )


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile (0 for an empty list)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def hour_start(moment: datetime) -> datetime:
    moment = moment.astimezone(UTC)
    return moment.replace(minute=0, second=0, microsecond=0)


def day_start(moment: datetime) -> datetime:
    return hour_start(moment).replace(hour=0)


@dataclass
class _Pending:
    values: dict[tuple[datetime, str, str], float] = field(default_factory=dict)


class MetricsHub:
    """Live aggregates over the last day, and the increments waiting to be saved."""

    def __init__(self, *, now=None) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._items: deque[RequestObservation] = deque(maxlen=KEEP)
        self._pending = _Pending()
        # tokens already saved for a day before this process started (see `seed_tokens`)
        self._seed_day: float | None = None
        self._seed_tokens = 0

    def seed_tokens(self, day: datetime, tokens: int) -> None:
        """At start-up: the tokens the saved hourly rows hold for `day`. Without this the day's
        total restarts at zero with every restart and the budget rule goes quiet."""
        with self._lock:
            self._seed_day = day_start(day).timestamp()
            self._seed_tokens = max(0, int(tokens))

    # ---- feeding -------------------------------------------------------------------
    def observe(self, obs: RequestObservation) -> None:
        with self._lock:
            self._items.append(obs)
            role = obs.role if obs.role in ROLES else None
            hour = hour_start(datetime.fromtimestamp(obs.at, UTC))
            add = {
                "requests": 1,
                "answered": 1 if obs.kind in ("answer", "reply") else 0,
                "refused": 1 if obs.kind == "refusal" else 0,
                "unavailable": 1 if obs.kind == "unavailable" else 0,
                "cached": 1 if obs.cached else 0,
                "degraded": 1 if obs.degraded else 0,
                "low_confidence": 1 if obs.low_confidence else 0,
                "tokens_in": obs.tokens_in,
                "tokens_out": obs.tokens_out,
                "billed_cost": obs.billed,
                "shadow_cost": obs.shadow,
            }
            for metric, value in add.items():
                if not value:
                    continue
                for who in {"all", *([role] if role else [])}:
                    key = (hour, who, metric)
                    self._pending.values[key] = self._pending.values.get(key, 0.0) + float(value)

    # ---- reading -------------------------------------------------------------------
    def window(self, seconds: float, *, role: str | None = None) -> list[RequestObservation]:
        cutoff = self._now().timestamp() - seconds
        with self._lock:
            items = list(self._items)
        return [o for o in items if o.at >= cutoff and (role is None or o.role == role)]

    def usage(self, seconds: float, *, role: str | None = None) -> dict[str, Any]:
        """Tokens per request (p50, p95, p99), cost per answer, the input/output split and the
        tokens per stage. Billed and shadow cost are separate numbers, always."""
        items = self.window(seconds, role=role)
        tokens = [float(o.tokens) for o in items if o.tokens]
        answers = [o for o in items if o.kind in ("answer", "reply")]
        billed = sum(o.billed for o in items)
        shadow = sum(o.shadow for o in items)
        per_stage: dict[str, int] = {}
        for item in items:
            for stage, used in item.stage_tokens:
                per_stage[stage] = per_stage.get(stage, 0) + used
        return {
            "requests": len(items),
            "tokens_p50": percentile(tokens, 0.50),
            "tokens_p95": percentile(tokens, 0.95),
            "tokens_p99": percentile(tokens, 0.99),
            "tokens_in": sum(o.tokens_in for o in items),
            "tokens_out": sum(o.tokens_out for o in items),
            "billed_cost": round(billed, 6),
            "shadow_cost": round(shadow, 6),
            "billed_per_answer": round(billed / len(answers), 6) if answers else 0.0,
            "shadow_per_answer": round(shadow / len(answers), 6) if answers else 0.0,
            "tokens_per_stage": dict(sorted(per_stage.items())),
        }

    def outcomes(self, seconds: float, *, role: str | None = None) -> dict[str, Any]:
        items = self.window(seconds, role=role)
        total = len(items)

        def share(count: int) -> float:
            return round(count / total, 4) if total else 0.0

        return {
            "requests": total,
            "answered": sum(1 for o in items if o.kind in ("answer", "reply")),
            "refused": sum(1 for o in items if o.kind == "refusal"),
            "unavailable": sum(1 for o in items if o.kind == "unavailable"),
            "cache_hit_rate": share(sum(1 for o in items if o.cached)),
            "degraded_rate": share(sum(1 for o in items if o.degraded)),
            "low_confidence_rate": share(sum(1 for o in items if o.low_confidence)),
        }

    def decider_shares(self, seconds: float) -> dict[str, dict[str, float]]:
        """Per step: the share of decisions each decider made, from the requests seen here."""
        counts: dict[str, dict[str, int]] = {}
        for item in self.window(seconds):
            for step, decider in item.deciders:
                counts.setdefault(step, {}).setdefault(decider, 0)
                counts[step][decider] += 1
        out: dict[str, dict[str, float]] = {}
        for step, by_decider in counts.items():
            total = sum(by_decider.values())
            out[step] = {d: round(n / total, 4) for d, n in sorted(by_decider.items())}
        return dict(sorted(out.items()))

    def tokens_today(self) -> int:
        start = day_start(self._now()).timestamp()
        with self._lock:
            live = sum(o.tokens for o in self._items if o.at >= start)
            seed = self._seed_tokens if self._seed_day == start else 0
            return seed + live

    # ---- saving --------------------------------------------------------------------
    def restore(self, pending: dict[tuple[datetime, str, str], float]) -> None:
        """Put increments back that could not be saved, so the next pass saves them."""
        with self._lock:
            for key, value in pending.items():
                self._pending.values[key] = self._pending.values.get(key, 0.0) + value

    def take_pending(self) -> dict[tuple[datetime, str, str], float]:
        with self._lock:
            taken, self._pending = self._pending.values, _Pending()
        return taken


# ---- the tables ------------------------------------------------------------------------------
def add_hourly(db: Database, increments: dict[tuple[datetime, str, str], float]) -> int:
    """Add increments to the hourly rows (one statement per row; creates a row on first use)."""
    written = 0
    with db.transaction(write=True):
        for (hour, role, metric), value in increments.items():
            db.execute(
                "INSERT INTO metric_rollups (id, period, period_start, role, metric, value) "
                "VALUES (?, 'hour', ?, ?, ?, ?) "
                "ON CONFLICT (period, period_start, role, metric) DO UPDATE SET "
                "value = metric_rollups.value + excluded.value",
                (str(uuid.uuid4()), utc_text(hour), role, metric, float(value)),
            )
            written += 1
    return written


def set_daily(db: Database, day: datetime, values: dict[tuple[str, str], float]) -> int:
    """Replace the daily rows of one UTC day. Keys are (role, metric)."""
    start = utc_text(day_start(day))
    with db.transaction(write=True):
        for (role, metric), value in values.items():
            db.execute(
                "INSERT INTO metric_rollups (id, period, period_start, role, metric, value) "
                "VALUES (?, 'day', ?, ?, ?, ?) "
                "ON CONFLICT (period, period_start, role, metric) DO UPDATE SET "
                "value = excluded.value",
                (str(uuid.uuid4()), start, role, metric, float(value)),
            )
    return len(values)


def daily_from_tables(db: Database, day: datetime) -> dict[tuple[str, str], float]:
    """The counts of one UTC day per role, straight from the tables. Counts only: the distinct
    users are counted here and not kept."""
    start = utc_text(day_start(day))
    end = utc_text(day_start(day) + timedelta(days=1))
    values: dict[tuple[str, str], float] = {}
    with db.transaction():
        for role in ROLES:

            def one(sql: str, params: tuple, role=role) -> float:
                row = db.query_one(sql, params)
                return float(row[0]) if row and row[0] is not None else 0.0

            window = (role, start, end)
            values[(role, "active_users")] = one(
                "SELECT COUNT(DISTINCT s.user_id) FROM messages m "
                "JOIN sessions s ON s.id = m.session_id JOIN users u ON u.id = s.user_id "
                "WHERE u.role = ? AND m.role = 'user' AND m.created_at >= ? AND m.created_at < ?",
                window,
            )
            values[(role, "chats_started")] = one(
                "SELECT COUNT(*) FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE u.role = ? AND s.created_at >= ? AND s.created_at < ?",
                window,
            )
            values[(role, "questions")] = one(
                "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id = m.session_id "
                "JOIN users u ON u.id = s.user_id WHERE u.role = ? AND m.role = 'user' "
                "AND m.created_at >= ? AND m.created_at < ?",
                window,
            )
            values[(role, "answers_verified")] = one(
                "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id = m.session_id "
                "JOIN users u ON u.id = s.user_id WHERE u.role = ? AND m.role = 'assistant' "
                "AND m.grounded = ? AND m.fixed_reply = ? AND m.citations <> '[]' "
                "AND m.created_at >= ? AND m.created_at < ?",
                (role, True, False, start, end),
            )
            values[(role, "fixed_replies")] = one(
                "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id = m.session_id "
                "JOIN users u ON u.id = s.user_id WHERE u.role = ? AND m.role = 'assistant' "
                "AND m.fixed_reply = ? AND m.created_at >= ? AND m.created_at < ?",
                (role, True, start, end),
            )
            for rating in ("up", "down"):
                values[(role, f"thumbs_{rating}")] = one(
                    "SELECT COUNT(*) FROM feedback_entries f JOIN users u ON u.id = f.user_id "
                    "WHERE u.role = ? AND f.rating = ? AND f.created_at >= ? AND f.created_at < ?",
                    (role, rating, start, end),
                )
        seats = db.query_one(
            "SELECT COUNT(*) FROM users WHERE role = 'staff' AND is_active = ?", (True,)
        )
        seat_count = float(seats[0]) if seats else 0.0
        active_staff = values[("staff", "active_users")]
        values[("staff", "seat_utilisation")] = (
            round(active_staff / seat_count, 4) if seat_count else 0.0
        )
        values[("customer", "seat_utilisation")] = 0.0
    return values


def _utc_day_expression(db: Database) -> str:
    """SQL for the UTC calendar day (YYYY-MM-DD) of `period_start`. Postgres would cast a
    timestamptz in the SESSION's time zone, so it is converted to UTC explicitly; SQLite holds the
    UTC text already."""
    if db.kind == "postgres":
        return "to_char(period_start AT TIME ZONE 'UTC', 'YYYY-MM-DD')"
    return "substr(CAST(period_start AS TEXT), 1, 10)"


def daily_series(db: Database, days: int, *, now: datetime) -> list[dict[str, Any]]:
    """The last `days` UTC days (oldest first), per role (customer, staff, all), from the saved
    rows: the day counts and the sums of that day's hourly rows, plus the ratios. Numbers only."""
    first = day_start(now) - timedelta(days=max(days, 1) - 1)
    by_day: dict[str, dict[str, dict[str, float]]] = {}
    with db.transaction():
        rows = db.query_all(
            "SELECT period_start, role, metric, value FROM metric_rollups "
            "WHERE period = 'day' AND period_start >= ? ORDER BY period_start, role, metric",
            (utc_text(first),),
        )
        day = _utc_day_expression(db)
        hours = db.query_all(
            f"SELECT {day}, role, metric, SUM(value) "
            "FROM metric_rollups WHERE period = 'hour' AND period_start >= ? "
            f"GROUP BY {day}, role, metric",
            (utc_text(first),),
        )
    for start, role, metric, value in rows:
        if hasattr(start, "astimezone"):  # a Postgres timestamptz: its UTC day
            start = start.astimezone(UTC).strftime("%Y-%m-%d")
        by_day.setdefault(str(start)[:10], {}).setdefault(role, {})[metric] = float(value)
    for label, role, metric, value in hours:
        by_day.setdefault(str(label), {}).setdefault(role, {})[metric] = float(value or 0.0)
    out = []
    for label, roles in sorted(by_day.items()):
        for values in roles.values():
            values.update(derived(values))
        out.append({"day": label, "roles": roles})
    return out


def hourly_totals(db: Database, start: datetime, end: datetime) -> dict[tuple[str, str], float]:
    """Sum the hourly rows of [start, end) per (role, metric)."""
    rows = db.query_all(
        "SELECT role, metric, SUM(value) FROM metric_rollups WHERE period = 'hour' "
        "AND period_start >= ? AND period_start < ? GROUP BY role, metric",
        (utc_text(start), utc_text(end)),
    )
    return {(r[0], r[1]): float(r[2] or 0.0) for r in rows}


def share(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def derived(values: dict[str, float]) -> dict[str, float]:
    """Ratios of one role's day: the thumbs-up share, the cache hit rate, tokens and cost per
    answer. (There is no "closed with a Helpful rating" proxy and no time-saved number.)"""
    up, down = values.get("thumbs_up", 0.0), values.get("thumbs_down", 0.0)
    answered = values.get("answered", 0.0)
    return {
        "thumbs_up_share": share(up, up + down),
        "cache_hit_rate": share(values.get("cached", 0.0), values.get("requests", 0.0)),
        "verified_answer_share": share(values.get("answers_verified", 0.0), answered),
        "tokens_per_answer": round(
            (values.get("tokens_in", 0.0) + values.get("tokens_out", 0.0)) / answered, 1
        )
        if answered
        else 0.0,
        "billed_per_answer": round(values.get("billed_cost", 0.0) / answered, 6)
        if answered
        else 0.0,
        "shadow_per_answer": round(values.get("shadow_cost", 0.0) / answered, 6)
        if answered
        else 0.0,
    }


def iter_roles() -> Iterable[str]:
    return ROLES
