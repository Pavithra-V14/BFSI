"""The estimate and the code of a REAL-provider load run. No call is made.

    uv run python scripts/load_estimate.py [--steps 10x60,20x60]

It prints the plan, how many questions it sends, the requests and tokens per provider against the
free-tier daily limits (the same table as an evaluation estimate), and the confirmation code. A
real run starts only with that code in `LOAD_CONFIRM`, and only after the owner has said yes
(spec 16, build rule 4). The stubbed mode needs none of this.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from load import plan  # noqa: E402


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(description="estimate of a real-provider load run")
    parser.add_argument("--steps", help="users x seconds, e.g. 10x60,20x60 (default: the plan)")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        steps = plan.parse_steps(args.steps if args.steps is not None else env.get("LOAD_STEPS"))
    except plan.PlanError as error:
        out(f"error: {error}")
        return 1

    from app.eval import constants as c
    from app.eval.cases import load_cases
    from app.eval.estimate import describe
    from app.gateway import GatewayConfig
    from app.gateway.embeddings import running_on_render

    if running_on_render(env):
        out("refused: load tests run on the admin machine only, never on Render")
        return 1
    try:
        config = GatewayConfig.from_env(env)
    except Exception as error:  # a missing or bad setting: its class only, never a value
        out(f"error: the gateway settings are not usable here ({type(error).__name__})")
        return 1
    _version, cases = load_cases()
    estimate = plan.real_estimate(steps, config, cases)
    out("plan: " + ", ".join(f"{s.users} users for {s.seconds} s" for s in steps))
    out(f"about {plan.question_count(steps)} chat questions in all")
    for line in describe(estimate):
        out(line)
    if estimate["too_big_for_one_day"] or len(estimate["batches"]) > 1:
        out("this plan does not fit one day of the free-tier limits: shorten it; no ticket is made")
        return 2
    ticket = plan.issue_ticket(estimate, steps)
    out(
        f"The ticket is for THIS plan and for today (UTC {ticket.day}), is valid for "
        f"{c.CONFIRMATION_MINUTES} minutes from now, and works ONCE: the real server uses it up "
        "when it starts."
    )
    out("It needs your explicit yes. If you say yes, set these three in the window of both "
        "servers and of Locust, and start load/real_server.py first:")  # fmt: skip
    out(f"  LOAD_MODE=real  LOAD_STEPS={ticket.steps}  LOAD_CONFIRM={ticket.code}")
    out("a real run writes to this machine only (a SQLite file, an in-process Redis stand-in) "
        "and only reads the test collection of the vector store")  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
