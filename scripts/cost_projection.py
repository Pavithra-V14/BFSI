"""A cost projection for a stated number of queries a day (spec 18). No call is made.

    uv run python scripts/cost_projection.py [--queries-per-day 100] [--share-follow-up 0.2]

Per day and per 30 days it prints the requests and tokens each provider group would see, the
shadow cost (what the same tokens would cost at list price: `app/gateway/pricing.json`), and the
billed cost on the free tiers, which is expected to be 0. The per-call sizes are the [T]
placeholders of the evaluation estimator (`app/eval/estimate.py`); they become measured numbers at
the Phase 16 baseline, when this projection is compared with a dry run (spec 17.6).

The number of queries a day is a [T] placeholder: 100 (spec 18).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INPUT_SHARE = 0.85  # [T] share of a call's tokens that are input (the prompt with its passages)


def project(queries_per_day: int, share_follow_up: float = 0.2) -> dict[str, dict[str, float]]:
    """{group: {requests, tokens} per day}, from the per-call sizes of the evaluation estimator."""
    from app.eval import estimate as est

    follow = max(0.0, min(1.0, share_follow_up))
    day: dict[str, dict[str, float]] = {}

    def add(who: str, requests: float, tokens: float) -> None:
        row = day.setdefault(who, {"requests": 0.0, "tokens": 0.0})
        row["requests"] += requests
        row["tokens"] += tokens

    for who, requests, tokens in (
        est.TURN_TRIAGE,
        est.TURN_EMBED,
        est.TURN_RERANK,
        est.TURN_GENERATE,
    ):
        add(who, requests * queries_per_day, tokens * queries_per_day)
    who, requests, tokens = est.TURN_REWRITE
    add(who, requests * queries_per_day * follow, tokens * queries_per_day * follow)
    return day


def shadow_costs(day: dict[str, dict[str, float]]) -> dict[str, float]:
    """Dollars per day at list price for the groups that have a price (answer, Jev)."""
    from app.gateway.config import DEFAULT_MODELS
    from app.gateway.cost import load_pricing, shadow_cost

    pricing = load_pricing()
    models = {"answer": "groq/" + DEFAULT_MODELS["GROQ_MODEL"], "jev": DEFAULT_MODELS["JEV_MODEL"]}
    out: dict[str, float] = {}
    for who, model in models.items():
        tokens = int(day.get(who, {}).get("tokens", 0))
        cost, _source = shadow_cost(
            model, int(tokens * INPUT_SHARE), int(tokens * (1 - INPUT_SHARE)), pricing
        )
        out[who] = cost
    return out


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description="a cost projection, no call")
    parser.add_argument("--queries-per-day", type=int, default=100)
    parser.add_argument("--share-follow-up", type=float, default=0.2)
    parser.add_argument("--json", action="store_true", help="print the numbers as JSON")
    args = parser.parse_args(argv)
    if not 1 <= args.queries_per_day <= 1_000_000:
        out("error: --queries-per-day must be from 1 to 1,000,000")
        return 1
    day = project(args.queries_per_day, args.share_follow_up)
    shadow = shadow_costs(day)
    if args.json:
        out(json.dumps({"day": day, "shadow_cost_day": shadow}, indent=2))
        return 0
    follow = f"{args.share_follow_up:.0%}"
    out(f"queries a day: {args.queries_per_day} [T placeholder]; follow-ups: {follow}")
    out("group | requests/day | tokens/day | tokens/30 days")
    for who in sorted(day):
        row = day[who]
        out(f"{who} | {row['requests']:.0f} | {row['tokens']:.0f} | {row['tokens'] * 30:.0f}")
    total = sum(shadow.values())
    out(f"shadow cost: ${total:.4f} a day, ${total * 30:.2f} for 30 days (list price, [T] sizes)")
    out("billed cost on the free tiers: expected $0 (spec 18); a provider over its daily limit "
        "falls to the next one, it is not billed")  # fmt: skip
    out("the per-call sizes are placeholders: Phase 16 compares this with a dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
