"""The load plan: the user steps, what a real-provider run would cost, the isolation rules of a
real run, and the single-use ticket that must be presented to start one. No locust import here,
so tests and the estimate script can use it.

Spec 16: "start at 10 concurrent users and step up, reporting where it breaks. Two modes:
stubbed model (safe) and real providers (uses quota). Always ask the owner before each run;
show a token estimate first; real-provider runs need an explicit yes."

Two modes:
- `stub`: the local stub server (`load/stub_server.py`): a scripted model, an in-memory vector
  store and a SQLite file. No key, no quota, no network.
- `real`: `load/real_server.py` on loopback with the owner's real providers, a LOCAL SQLite
  database, an in-process Redis stand-in, and only a test (`_eval`) collection of the cloud vector
  store, read only. It starts only with the ticket of this exact plan (see `Ticket`).

The ticket: made by `scripts/load_estimate.py` for one plan on one UTC day; valid for
`CONFIRMATION_MINUTES` from its issue; usable once (the real server consumes it).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "load_out"

# [P] the step plan: 10 users first (spec 16), then doubling; one minute each
DEFAULT_STEPS: tuple[tuple[int, int], ...] = ((10, 60), (20, 60), (40, 60), (80, 60), (160, 60))
MEAN_WAIT_SECONDS = 5.5  # the locust file waits 3 to 8 seconds between questions
ERROR_RATE_LIMIT = 0.01  # [T] "breaks" = more than 1 % of requests failing or unavailable
P95_LIMIT_MS = 30_000  # [T] ... or a p95 above the 30 s request budget (spec 4)
ACTIVE_SHARE_MIN = 0.80  # [T] ... or fewer than 80 % of the step's users actually active
MAX_USERS = 500  # a plan above this is refused: this is a pilot-size tool
MIN_SECONDS = 20  # Locust's current percentile looks back 10 s: a shorter step has no clean data
MAX_SECONDS = 3_600
LOOPBACK = ("127.0.0.1", "localhost", "::1")
ENV_COLLECTION = "LOAD_COLLECTION"


class PlanError(ValueError):
    """A bad plan, an isolation problem or a bad ticket. The message is safe to print."""


@dataclass(frozen=True)
class Step:
    users: int
    seconds: int


def parse_steps(text: str | None) -> tuple[Step, ...]:
    """`10x60,20x60` -> steps of (users, seconds). Empty means the default plan."""
    if not text or not text.strip():
        return tuple(Step(u, s) for u, s in DEFAULT_STEPS)
    steps: list[Step] = []
    for part in text.split(","):
        try:
            users, seconds = (int(x) for x in part.strip().lower().split("x"))
        except ValueError:
            raise PlanError("LOAD_STEPS must look like 10x60,20x60 (users x seconds)") from None
        if users < 1 or users > MAX_USERS or seconds < MIN_SECONDS or seconds > MAX_SECONDS:
            raise PlanError(
                f"each step needs 1 to {MAX_USERS} users and {MIN_SECONDS} to {MAX_SECONDS} seconds"
            )
        steps.append(Step(users, seconds))
    if steps[0].users != 10:
        raise PlanError("the first step is 10 users (spec 16)")
    if any(b.users <= a.users for a, b in zip(steps, steps[1:], strict=False)):
        raise PlanError("the steps must go up")
    return tuple(steps)


def steps_text(steps: Sequence[Step]) -> str:
    return ",".join(f"{s.users}x{s.seconds}" for s in steps)


def max_users(steps: Sequence[Step]) -> int:
    return max(s.users for s in steps)


def question_count(steps: Sequence[Step], mean_wait: float = MEAN_WAIT_SECONDS) -> int:
    """About how many chat questions the plan sends (users x seconds / mean wait)."""
    return round(sum(s.users * s.seconds / mean_wait for s in steps))


def real_estimate(steps: Sequence[Step], config: Any, cases: Sequence[Any]) -> dict[str, Any]:
    """Requests and tokens per provider group for a real run, in the shape of an evaluation
    estimate (so the same table and the same daily-limit check apply)."""
    from app.eval.estimate import estimate_run

    wanted = question_count(steps)
    pool = [c for c in cases if getattr(c, "active", False) and c.role == "customer"] or list(cases)
    if not pool:
        raise PlanError("there is no question to send")
    spread = [pool[i % len(pool)] for i in range(wanted)]
    return estimate_run(spread, config, judged=False)


def confirmation(estimate: Mapping[str, Any], steps: Sequence[Step], day: str) -> str:
    """The code of this exact plan on this UTC day (`YYYY-MM-DD`): it changes with the steps,
    the numbers and the date."""
    from app.eval.estimate import confirmation_code

    return confirmation_code(
        estimate, kind=f"load:{steps_text(steps)}:{day}", dataset_version="load-v1"
    )


# ================================================================================ isolation
def hostname_of(host: str) -> str:
    """The host name of a URL or `host:port`, lower case, without brackets."""
    text = (host or "").strip()
    parts = urlsplit(text if "//" in text else "//" + text)
    return (parts.hostname or "").lower()


def port_of(host: str, default: int = 8099) -> int:
    text = (host or "").strip()
    parts = urlsplit(text if "//" in text else "//" + text)
    return parts.port or default


def is_loopback(host: str) -> bool:
    """Equality of the host name, not a substring test: `localhost.evil.example` is not local."""
    return hostname_of(host) in LOOPBACK


def collection_from(env: Mapping[str, str]) -> str:
    from app.eval import constants as c

    raw = (env.get(ENV_COLLECTION) or env.get("EVAL_COLLECTION") or c.EVAL_COLLECTION).strip()
    return raw


def real_isolation_problems(env: Mapping[str, str] | None = None) -> list[str]:
    """Why a real run must not start with these settings, as plain lines (names only).

    A real run writes to this machine only: a SQLite file and an in-process Redis stand-in. The
    cloud database and the cloud cache are refused even if the run would not use them (the same
    guard as the evaluation); the cloud vector store is allowed for one thing: reading an `_eval`
    test collection."""
    from app.eval import constants as c
    from app.isolation import remote_variables

    env = os.environ if env is None else env
    problems = [
        f"{name} points at a remote host: unset it in this window (a load run never uses it)"
        for name in remote_variables(env)
        if name != "QDRANT_URL"
    ]
    try:
        c.check_eval_collection(collection_from(env))
    except c.EvalConfigError as error:
        problems.append(f"the collection is refused: {error}")
    return problems


# ================================================================================== the ticket
@dataclass(frozen=True)
class Ticket:
    code: str
    steps: str
    day: str
    issued_at: str
    used: bool


def ticket_path(folder: Path | None = None) -> Path:
    return (folder or OUT_DIR) / "load_ticket.json"


def marker_path(folder: Path | None = None) -> Path:
    return (folder or OUT_DIR) / "real_server.json"


def users_path(folder: Path | None = None) -> Path:
    return (folder or OUT_DIR) / "load_users.json"


def _utc(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def issue_ticket(
    estimate: Mapping[str, Any],
    steps: Sequence[Step],
    *,
    now: datetime | None = None,
    folder: Path | None = None,
) -> Ticket:
    """Make the ticket of this plan for today and write it (replacing any earlier one)."""
    moment = _utc(now)
    day = moment.strftime("%Y-%m-%d")
    ticket = Ticket(
        code=confirmation(estimate, steps, day),
        steps=steps_text(steps),
        day=day,
        issued_at=moment.isoformat(),
        used=False,
    )
    path = ticket_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ticket.__dict__, indent=2) + "\n", "utf-8")
    return ticket


def _read_ticket(folder: Path | None) -> Ticket:
    path = ticket_path(folder)
    if not path.is_file():
        raise PlanError(
            "there is no ticket: run scripts/load_estimate.py first and say yes to its estimate"
        )
    try:
        data = json.loads(path.read_text("utf-8"))
        return Ticket(
            str(data["code"]), str(data["steps"]), str(data["day"]),
            str(data["issued_at"]), bool(data["used"]),
        )  # fmt: skip
    except (ValueError, KeyError, TypeError):
        raise PlanError("the ticket file is not readable: make a new estimate") from None


def consume_ticket(
    given: str,
    steps: Sequence[Step],
    *,
    now: datetime | None = None,
    folder: Path | None = None,
) -> Ticket:
    """Check the code against the ticket and use it up. Refused (nothing is started, the ticket
    is left as it is) when the code is missing or wrong, belongs to another plan or another day,
    is older than the window, or was used before."""
    from app.eval import constants as c

    ticket = _read_ticket(folder)
    moment = _utc(now)
    code = (given or "").strip().upper()
    if not code:
        raise PlanError(
            "a real-provider run needs LOAD_CONFIRM: run scripts/load_estimate.py first and "
            "type the code it prints (it needs your explicit yes)"
        )
    if code != ticket.code.upper():
        raise PlanError("LOAD_CONFIRM does not match the ticket: make a new estimate")
    if ticket.steps != steps_text(steps):
        raise PlanError("LOAD_STEPS is not the plan of the ticket: make a new estimate")
    if ticket.day != moment.strftime("%Y-%m-%d"):
        raise PlanError("the ticket is from another day (UTC): make a new estimate")
    issued = datetime.fromisoformat(ticket.issued_at)
    if moment - issued > timedelta(minutes=c.CONFIRMATION_MINUTES):
        raise PlanError(
            f"the ticket is older than {c.CONFIRMATION_MINUTES} minutes: make a new estimate"
        )
    if ticket.used:
        raise PlanError("this ticket was already used (a code works once): make a new estimate")
    used = Ticket(ticket.code, ticket.steps, ticket.day, ticket.issued_at, True)
    ticket_path(folder).write_text(json.dumps(used.__dict__, indent=2) + "\n", "utf-8")
    return used


def write_marker(
    ticket: Ticket, port: int, *, now: datetime | None = None, folder: Path | None = None
) -> None:
    """The real server says which plan and which port it consumed the ticket for."""
    path = marker_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "code": ticket.code,
        "steps": ticket.steps,
        "port": port,
        "started_at": _utc(now).isoformat(),
    }
    path.write_text(json.dumps(body, indent=2) + "\n", "utf-8")


def check_marker(
    given: str, steps: Sequence[Step], host: str, *, folder: Path | None = None
) -> None:
    """Locust side of a real run: the target must be the real server that consumed THIS ticket
    (same code, same plan, same port); anything else is refused."""
    path = marker_path(folder)
    if not path.is_file():
        raise PlanError("no real server is running for this plan: start load/real_server.py first")
    try:
        marker = json.loads(path.read_text("utf-8"))
    except ValueError:
        raise PlanError("the real server's marker is not readable: start it again") from None
    if str(marker.get("code", "")).upper() != (given or "").strip().upper():
        raise PlanError("LOAD_CONFIRM is not the code the real server was started with")
    if marker.get("steps") != steps_text(steps):
        raise PlanError("LOAD_STEPS is not the plan the real server was started with")
    if int(marker.get("port", -1)) != port_of(host):
        raise PlanError("the host is not the port of the real server that holds this ticket")


def check_start(
    env: Mapping[str, str] | None = None,
) -> tuple[str, tuple[Step, ...]]:
    """The mode and steps of a run, or `PlanError`. Real mode needs the isolation rules to hold in
    this window too and a code (the host and the server marker are checked when the test starts,
    where the host is known)."""
    env = os.environ if env is None else env
    mode = (env.get("LOAD_MODE") or "stub").strip().lower()
    if mode not in ("stub", "real"):
        raise PlanError("LOAD_MODE must be stub or real")
    steps = parse_steps(env.get("LOAD_STEPS"))
    if mode == "stub":
        return mode, steps
    if not (env.get("LOAD_CONFIRM") or "").strip():
        raise PlanError(
            "a real-provider run needs LOAD_CONFIRM: run scripts/load_estimate.py first and "
            "type the code it prints (it needs your explicit yes)"
        )
    problems = real_isolation_problems(env)
    if problems:
        raise PlanError("; ".join(problems))
    return mode, steps


def mode_banner(mode: str) -> str:
    if mode == "stub":
        return (
            "STUBBED MODEL: a scripted model, a local store. This shows how the code behaves "
            "under concurrency; it does not show the capacity of any host."
        )
    return (
        "REAL PROVIDERS: this run uses free-tier quota and was confirmed by its ticket. It "
        "writes to this machine only (a SQLite file, an in-process Redis stand-in) and reads a "
        "test collection of the cloud vector store."
    )
