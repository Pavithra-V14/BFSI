"""The load plan: the user steps, what a real-provider run would cost, and the code that must be
typed to start one. No locust import here, so tests and the estimate script can use it.

Spec 16: "start at 10 concurrent users and step up, reporting where it breaks. Two modes:
stubbed model (safe) and real providers (uses quota). Always ask the owner before each run;
show a token estimate first; real-provider runs need an explicit yes."

Two modes:
- `stub`: the local stub server (`load/stub_server.py`): a scripted model, an in-memory vector
  store and a SQLite file. No key, no quota, no network.
- `real`: the owner's own local API with real providers. It needs the confirmation code of this
  exact plan, printed with its estimate by `scripts/load_estimate.py`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# [P] the step plan: 10 users first (spec 16), then doubling; one minute each
DEFAULT_STEPS: tuple[tuple[int, int], ...] = ((10, 60), (20, 60), (40, 60), (80, 60), (160, 60))
MEAN_WAIT_SECONDS = 5.5  # the locust file waits 3 to 8 seconds between questions
ERROR_RATE_LIMIT = 0.01  # [T] "breaks" = more than 1 % of requests failing in a step
P95_LIMIT_MS = 30_000  # [T] ... or a p95 above the 30 s request budget (spec 4)
MAX_USERS = 500  # a plan above this is refused: this is a pilot-size tool
MAX_SECONDS = 3_600


class PlanError(ValueError):
    """A bad plan or a missing confirmation. The message is safe to print."""


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
        if users < 1 or users > MAX_USERS or seconds < 10 or seconds > MAX_SECONDS:
            raise PlanError(
                f"each step needs 1 to {MAX_USERS} users and 10 to {MAX_SECONDS} seconds"
            )
        steps.append(Step(users, seconds))
    if steps[0].users != 10:
        raise PlanError("the first step is 10 users (spec 16)")
    if any(b.users <= a.users for a, b in zip(steps, steps[1:], strict=False)):
        raise PlanError("the steps must go up")
    return tuple(steps)


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


def confirmation(estimate: Mapping[str, Any], steps: Sequence[Step]) -> str:
    """The code of this exact plan: it changes when the steps or the numbers change."""
    from app.eval.estimate import confirmation_code

    shape = ",".join(f"{s.users}x{s.seconds}" for s in steps)
    return confirmation_code(estimate, kind="load:" + shape, dataset_version="load-v1")


def check_start(env: Mapping[str, str] | None = None) -> tuple[str, tuple[Step, ...]]:
    """The mode and steps of a run, or `PlanError`. In real mode the code of the plan must be
    given in `LOAD_CONFIRM`, and the estimate must fit what the free tiers allow in a day."""
    env = os.environ if env is None else env
    mode = (env.get("LOAD_MODE") or "stub").strip().lower()
    if mode not in ("stub", "real"):
        raise PlanError("LOAD_MODE must be stub or real")
    steps = parse_steps(env.get("LOAD_STEPS"))
    if mode == "stub":
        return mode, steps
    given = (env.get("LOAD_CONFIRM") or "").strip().upper()
    if not given:
        raise PlanError(
            "a real-provider run needs LOAD_CONFIRM: run scripts/load_estimate.py first and "
            "type the code it prints (it needs your explicit yes)"
        )
    from app.eval.cases import load_cases
    from app.gateway import GatewayConfig

    _version, cases = load_cases()
    estimate = real_estimate(steps, GatewayConfig.from_env(env), cases)
    if given != confirmation(estimate, steps):
        raise PlanError("LOAD_CONFIRM does not match this plan: make a new estimate")
    if estimate["too_big_for_one_day"] or len(estimate["batches"]) > 1:
        raise PlanError("this plan does not fit one day of the free-tier limits: shorten it")
    return mode, steps


def mode_banner(mode: str) -> str:
    if mode == "stub":
        return (
            "STUBBED MODEL: a scripted model, a local store. This shows how the code behaves "
            "under concurrency; it does not show the capacity of any host."
        )
    return "REAL PROVIDERS: this run uses free-tier quota and was confirmed by its code."
