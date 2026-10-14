"""The estimate every run must be confirmed against (owner additions a and b).

Before ANY call the runner shows what a run will cost: requests and tokens per provider, set
against the free-tier daily limits of the keys that would be used, and, when the run cannot fit in
one day, a plan of resumable batches. The run starts only when the owner types the confirmation
code printed with the estimate (the code is bound to these exact numbers, so a changed set, a
changed case list or a changed setting gives a different code).

The per-call sizes below are [T] placeholders (measured numbers replace them after the first
baseline run). They are deliberately on the high side: an estimate that is too low would make the
25 % guard stop a healthy run.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.eval import constants as c
from app.eval.cases import Case

# ---- [T] sizes of one call: (requests, tokens). Tokens are input plus output.
TURN_TRIAGE = ("jev", 1, 700)  # the decide-tier questions of stage 3
TURN_EMBED = ("embedding", 1, 120)
TURN_RERANK = ("rerank", 1, 2_500)
TURN_GENERATE = ("answer", 1, 3_600)  # prompt with five chunks plus the answer
TURN_REWRITE = ("answer", 1, 800)  # a follow-up's standalone question

# What the judge does for one answered case: text calls on the judge model, typed verdicts on Jev.
JUDGE_TEXT_CALLS = 18  # RAGAS faithfulness, relevancy (3), precision per chunk, recall, entities,
# correctness, one retry's worth of unreadable output, and G-Eval
JUDGE_TEXT_TOKENS = 1_800
JUDGE_VERDICT_CALLS = 3  # relevance of the chunks, support of the citations, the refusal
JUDGE_VERDICT_TOKENS = 1_200
JUDGE_EMBED_CALLS = 3

# An edit or a newer version of a clause in the test collection re-embeds that one clause.
SCENARIO_EMBED = ("embedding", 1, 200)

# These groups use the keys the shell holds for the production service: an evaluation run shares
# their provider daily limits with it (owner decision: accepted, stated in every estimate).
SHARED_WITH_PRODUCTION = ("jev", "embedding", "rerank")

PROVIDER_LABELS = {
    "answer": "answer models (Groq, Gemini)",
    "judge": "judge model (OpenRouter, evaluation keys)",
    "jev": "Jev (OpenRouter Decisions API)",
    "embedding": "embeddings (Gemini)",
    "rerank": "rerank (Cohere or Jina)",
}


@dataclass
class Need:
    requests: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    tokens: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, who: str, requests: int, tokens: int) -> None:
        self.requests[who] += requests
        self.tokens[who] += tokens

    def merge(self, other: Need) -> None:
        for who, n in other.requests.items():
            self.requests[who] += n
        for who, n in other.tokens.items():
            self.tokens[who] += n

    @property
    def total_requests(self) -> int:
        return sum(self.requests.values())

    @property
    def total_tokens(self) -> int:
        return sum(self.tokens.values())


def case_need(case: Case, *, judged: bool = True) -> Need:
    """What one case costs. Early exits are priced as the pipeline takes them: a greeting makes no
    call, an out-of-domain refusal stops after triage, a refusal never reaches the generator."""
    need = Need()
    turns = len(case.turns)
    for index in range(turns):
        last = index == turns - 1
        if case.behaviour == "greeting" and last and turns == 1:
            continue  # a greeting makes no call at all
        need.add(*TURN_TRIAGE)
        if index > 0:
            need.add(*TURN_REWRITE)
        if not last or case.behaviour == "answer":
            need.add(*TURN_EMBED)
            need.add(*TURN_RERANK)
            need.add(*TURN_GENERATE)
        elif case.behaviour == "refuse" and case.source_docs:
            need.add(*TURN_EMBED)  # a search that finds nothing visible, then no generation
            need.add(*TURN_RERANK)
    for op in case.setup:
        if op.op in ("edit_text", "supersede_text"):
            need.add(*SCENARIO_EMBED)
    if judged and case.behaviour == "answer":
        need.add("judge", JUDGE_TEXT_CALLS, JUDGE_TEXT_CALLS * JUDGE_TEXT_TOKENS)
        need.add("jev", JUDGE_VERDICT_CALLS, JUDGE_VERDICT_CALLS * JUDGE_VERDICT_TOKENS)
        need.add("embedding", JUDGE_EMBED_CALLS, JUDGE_EMBED_CALLS * 120)
    elif judged and case.behaviour in ("refuse", "block"):
        need.add("jev", 1, JUDGE_VERDICT_TOKENS)
    return need


# Owner decision (Phase 16, docs/READINESS.md "Terms of service"): one key per provider, from one
# account. A capacity plan must not assume pooled free keys, so a provider's daily limit is counted
# ONCE however many keys the pool holds (the pool is for rotation overlap and paid plans).
ONE_KEY_PER_PROVIDER = True


@dataclass(frozen=True)
class DailyLimits:
    """Per provider group: requests and tokens a day, from the key pool's own limits (None: the
    provider publishes none). Built from the gateway configuration."""

    requests: dict[str, int | None]
    tokens: dict[str, int | None]
    keys: dict[str, int]


def daily_limits(config: Any) -> DailyLimits:
    """The daily limits of the pools an evaluation run uses, from a `GatewayConfig`."""
    limits = config.limits

    def per_day(provider: str, keys: int) -> tuple[int | None, int | None]:
        lim = limits[provider]
        if ONE_KEY_PER_PROVIDER:
            keys = min(keys, 1)  # free allowances of several accounts are never added up
        rpd = lim.rpd * keys if getattr(lim, "rpd", None) else None
        tpd = lim.tpd * keys if getattr(lim, "tpd", None) else None
        return rpd, tpd

    groq_keys = max(len(config.groq_keys), 0)
    gemini_keys = max(len(config.gemini_keys), 0)
    judge_keys = len(config.eval_openrouter_keys)
    answer_rpd = [
        x for x in (per_day("GROQ", groq_keys)[0], per_day("GEMINI", gemini_keys)[0]) if x
    ]
    answer_tpd = [
        x for x in (per_day("GROQ", groq_keys)[1], per_day("GEMINI", gemini_keys)[1]) if x
    ]
    jr, jt = per_day("OPENROUTER", judge_keys)
    return DailyLimits(
        requests={
            # the answer pools are tried in turn: the most generous one bounds the day
            "answer": max(answer_rpd) if answer_rpd else None,
            "judge": jr,
            "jev": per_day("JEV", 1)[0],
            "embedding": per_day("EMBEDDING", max(len(config.embedding_keys), 1))[0],
            "rerank": per_day("COHERE" if config.cohere_key else "JINA", 1)[0],
        },
        tokens={
            "answer": max(answer_tpd) if answer_tpd else None,
            "judge": jt,
            "jev": None,
            "embedding": None,
            "rerank": None,
        },
        keys={
            "answer": max(groq_keys, gemini_keys),
            "judge": judge_keys,
            "jev": 1 if config.jev_key else 0,
            "embedding": len(config.embedding_keys),
            "rerank": 1 if (config.cohere_key or config.jina_key) else 0,
        },
    )


def fits(need: Need, limits: DailyLimits, share: float) -> bool:
    for who in set(need.requests) | set(need.tokens):
        cap_r = limits.requests.get(who)
        cap_t = limits.tokens.get(who)
        if cap_r is not None and need.requests.get(who, 0) > cap_r * share:
            return False
        if cap_t is not None and need.tokens.get(who, 0) > cap_t * share:
            return False
    return True


def plan_batches(
    cases: Sequence[Case], limits: DailyLimits, *, judged: bool = True,
    share: float = c.DAILY_LIMIT_SAFETY,
) -> tuple[list[list[str]], list[str]]:  # fmt: skip
    """Split the cases into batches that each fit one day's limits (at `share` of them).
    Returns (batches of case ids, ids of cases that do not fit a day on their own)."""
    batches: list[list[str]] = []
    too_big: list[str] = []
    current: list[str] = []
    running = Need()
    for case in cases:
        need = case_need(case, judged=judged)
        if not fits(need, limits, share):
            too_big.append(case.id)
        trial = Need()
        trial.merge(running)
        trial.merge(need)
        if current and not fits(trial, limits, share):
            batches.append(current)
            current, running = [], Need()
        current.append(case.id)
        running.merge(need)
    if current:
        batches.append(current)
    return batches, too_big


def estimate_run(
    cases: Sequence[Case],
    config: Any,
    *,
    judged: bool = True,
    minutes_per_request: float = 0.05,
) -> dict[str, Any]:
    limits = daily_limits(config)
    total = Need()
    for case in cases:
        total.merge(case_need(case, judged=judged))
    batches, too_big = plan_batches(cases, limits, judged=judged)
    providers: dict[str, Any] = {}
    for who in sorted(set(total.requests) | set(total.tokens)):
        daily_r, daily_t = limits.requests.get(who), limits.tokens.get(who)
        days = 1
        if daily_r:
            days = max(days, -(-total.requests.get(who, 0) // int(daily_r * c.DAILY_LIMIT_SAFETY)))
        if daily_t:
            days = max(days, -(-total.tokens.get(who, 0) // int(daily_t * c.DAILY_LIMIT_SAFETY)))
        providers[who] = {
            "label": PROVIDER_LABELS.get(who, who),
            "requests": total.requests.get(who, 0),
            "tokens": total.tokens.get(who, 0),
            "daily_request_limit": daily_r,
            "daily_token_limit": daily_t,
            "keys": limits.keys.get(who, 0),
            "days_needed": days,
        }
    return {
        "cases": len(cases),
        "turns": sum(len(case.turns) for case in cases),
        "judged": bool(judged),
        "providers": providers,
        "total_requests": total.total_requests,
        "total_tokens": total.total_tokens,
        "minutes": round(total.total_requests * minutes_per_request, 1),
        "batches": [list(b) for b in batches],
        "too_big_for_one_day": too_big,
        "margin": c.OVER_ESTIMATE_MARGIN,
        "shared_with_production": [g for g in SHARED_WITH_PRODUCTION if g in providers],
        "valid_minutes": c.CONFIRMATION_MINUTES,
    }


def confirmation_code(estimate: Mapping[str, Any], *, kind: str, dataset_version: str) -> str:
    """A short code bound to these exact numbers: AAAA-1111."""
    core = {
        "kind": kind,
        "dataset_version": dataset_version,
        "cases": [cid for batch in estimate["batches"] for cid in batch],
        "requests": estimate["total_requests"],
        "tokens": estimate["total_tokens"],
        "judged": estimate.get("judged", True),
    }
    digest = hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest().upper()
    letters = "".join(ch for ch in digest if ch.isalpha())[:4].ljust(4, "A")
    digits = "".join(ch for ch in digest if ch.isdigit())[:4].ljust(4, "0")
    return f"{letters}-{digits}"


def describe(estimate: Mapping[str, Any]) -> list[str]:
    """The estimate as plain lines for the terminal and the page: numbers and names only."""
    lines = [
        f"cases: {estimate['cases']} ({estimate['turns']} questions)",
        f"requests: {estimate['total_requests']}, tokens: {estimate['total_tokens']}, "
        f"about {estimate['minutes']} minutes",
    ]
    for row in estimate["providers"].values():
        cap_r = row["daily_request_limit"]
        cap_t = row["daily_token_limit"]
        limit = []
        if cap_r:
            limit.append(f"{cap_r} requests a day")
        if cap_t:
            limit.append(f"{cap_t} tokens a day")
        lines.append(
            f"- {row['label']}: {row['requests']} requests, {row['tokens']} tokens; "
            f"{row['keys']} key(s); limit {', '.join(limit) or 'not published'}; "
            f"{row['days_needed']} day(s)"
        )
    batches = estimate["batches"]
    if len(batches) > 1:
        lines.append(
            f"This does not fit one day: it is split into {len(batches)} batches "
            "(resume each one on its own day with the same confirmation code)."
        )
    if estimate["too_big_for_one_day"]:
        lines.append(
            f"{len(estimate['too_big_for_one_day'])} case(s) alone exceed a day's limit: "
            "add keys or accept that they will be cut off."
        )
    shared = estimate.get("shared_with_production") or []
    if shared:
        lines.append(
            "Shared with production: " + ", ".join(shared) + " use the same provider keys as the "
            "live service, so they share its daily limits; today's check counts what evaluation "
            "runs used of them."
        )
    lines.append(
        f"The run stops by itself when it uses more than {int(estimate['margin'] * 100)} % over "
        "this estimate."
    )
    minutes = estimate.get("valid_minutes")
    if minutes:
        lines.append(f"The confirmation code is valid for {minutes} minutes from this estimate.")
    return lines
