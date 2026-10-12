"""A cost projection for a stated number of queries a day (spec 18). No call is made.

    uv run python scripts/cost_projection.py [--queries-per-day 100] [--share-follow-up 0.2]

Per day and per 30 days it prints the requests and tokens each provider group would see, the
shadow cost of every group that has a list price (what the same tokens would cost:
`app/gateway/pricing.json`, else LiteLLM's price map), which groups have NO price (shown as
unpriced and left out of the total, never counted as $0), and the billed cost on the free tiers,
which is expected to be 0. The per-call sizes are the [T]
placeholders of the evaluation estimator (`app/eval/estimate.py`); they become measured numbers at
the Phase 16 baseline, when this projection is compared with a dry run (spec 17.6).

The number of queries a day is a [T] placeholder: 100 (spec 18).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class GroupCost:
    """The shadow cost of one provider group, or the reason it has none: an unpriced group is
    shown as unpriced and never counted as $0."""

    group: str
    model: str
    dollars: float | None  # per day; None when the group is not priced
    source: str  # where the price came from, or why there is none

    @property
    def priced(self) -> bool:
        return self.dollars is not None


# group -> (the model name the price is looked up under, share of the tokens that are input)
def _group_models() -> dict[str, tuple[str, float]]:
    from app.gateway.config import DEFAULT_MODELS

    return {
        "answer": ("groq/" + DEFAULT_MODELS["GROQ_MODEL"], INPUT_SHARE),
        "jev": (DEFAULT_MODELS["JEV_MODEL"], INPUT_SHARE),
        # an embedding or a rerank call has no output tokens: every token is input
        "embedding": ("gemini/" + DEFAULT_MODELS["GEMINI_EMBEDDING_MODEL"], 1.0),
        "rerank": ("cohere/" + DEFAULT_MODELS["COHERE_RERANK_MODEL"], 1.0),
    }


def shadow_costs(day: dict[str, dict[str, float]]) -> dict[str, GroupCost]:
    """Per group, at list price, for every group in `day`. A price counts only when it is the
    price OF THAT MODEL: an entry of `pricing.json`, or LiteLLM's own price for it. The reference
    price (the fallback for a model that is free or unknown) is that of ONE model, so it prices
    only that model; for any other it would be somebody else's price, and the group is reported
    as unpriced."""
    from app.gateway.cost import load_pricing, shadow_cost

    pricing = load_pricing()
    reference_model = pricing["default_reference"]["model"]
    models = _group_models()
    out: dict[str, GroupCost] = {}
    for who in sorted(day):
        if who not in models:
            out[who] = GroupCost(who, "-", None, "no model is assigned to this group here")
            continue
        model, input_share = models[who]
        tokens = int(day[who]["tokens"])
        cost, source = shadow_cost(
            model, int(tokens * input_share), int(tokens * (1 - input_share)), pricing
        )
        if source == "default_reference" and model != reference_model:
            why = (
                "billed per search, not per token; no price in pricing.json"
                if who == "rerank"
                else "no list price in pricing.json or LiteLLM's price map"
            )
            out[who] = GroupCost(who, model, None, why)
        elif source == "default_reference":
            out[who] = GroupCost(who, model, cost, "reference price (a placeholder to verify)")
        else:
            label = "pricing.json" if source == "pricing_table" else "LiteLLM's price map"
            out[who] = GroupCost(who, model, cost, label)
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
    priced = {who: g for who, g in shadow.items() if g.priced}
    unpriced = {who: g for who, g in shadow.items() if not g.priced}
    if args.json:
        out(
            json.dumps(
                {
                    "day": day,
                    "shadow_cost_day": {who: g.dollars for who, g in priced.items()},
                    "priced": {
                        who: {"model": g.model, "dollars_day": g.dollars, "source": g.source}
                        for who, g in priced.items()
                    },
                    "unpriced": {
                        who: {"model": g.model, "reason": g.source} for who, g in unpriced.items()
                    },
                },
                indent=2,
            )
        )
        return 0
    follow = f"{args.share_follow_up:.0%}"
    out(f"queries a day: {args.queries_per_day} [T placeholder]; follow-ups: {follow}")
    out("group | requests/day | tokens/day | tokens/30 days")
    for who in sorted(day):
        row = day[who]
        out(f"{who} | {row['requests']:.0f} | {row['tokens']:.0f} | {row['tokens'] * 30:.0f}")
    out("shadow cost by group (list price of the default models, [T] sizes):")
    for who, g in shadow.items():
        if g.priced:
            out(f"  {who}: ${g.dollars:.4f} a day ({g.model}; {g.source})")
        else:
            out(f"  {who}: UNPRICED ({g.model}): {g.source}")
    total = sum(g.dollars for g in priced.values() if g.dollars is not None)
    names = ", ".join(sorted(priced)) or "none"
    out(f"priced groups ({names}): ${total:.4f} a day, ${total * 30:.2f} for 30 days")
    if unpriced:
        out(
            "NOT INCLUDED, no price known: "
            + ", ".join(sorted(unpriced))
            + ". The total above is a lower bound, not the whole shadow cost."
        )
    out("billed cost on the free tiers: expected $0 (spec 18); a provider over its daily limit "
        "falls to the next one, it is not billed")  # fmt: skip
    out("the per-call sizes are placeholders: Phase 16 compares this with a dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
