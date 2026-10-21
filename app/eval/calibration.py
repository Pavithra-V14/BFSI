"""Calibration (spec 15.2, 15.3): how well do the judge model and Jev agree with the hand labels?

30 items, five kinds of yes/no question (`claim_supported`, `refusal_correct`, `chunk_relevant`,
`citation_supports`, `injection_call_right`). Each item is put to the judge model alone and to Jev
alone. For a metric, agreement is the share of items where the verdict equals the label.

Rule (code): Jev decides a metric's verdicts in an evaluation only when its agreement with the
labels on that metric reached the threshold (85 % [T] placeholder). Anything under it, and a
metric never calibrated, goes to the judge model. A verdict nobody could give does not count
for or against.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from app.db.connection import Database
from app.eval import constants as c
from app.eval import store
from app.eval.cases import CalibrationItem
from app.eval.estimate import PROVIDER_LABELS, Need
from app.eval.guard import CostGuard
from app.eval.judge import Judge
from app.gateway.config import effective_judge_model


def agreement(pairs: Sequence[tuple[bool | None, bool]]) -> tuple[float | None, int]:
    """(share of verdicts equal to the label, how many verdicts counted). None verdicts (not
    scored) are left out."""
    counted = [(v, label) for v, label in pairs if v is not None]
    if not counted:
        return None, 0
    return sum(1 for v, label in counted if v == label) / len(counted), len(counted)


def trusted(
    jev_agreement: float | None,
    items: int,
    counted: int | None = None,
    threshold: float = c.CALIBRATION_THRESHOLD,
) -> bool:
    """Jev is trusted for a metric only when (a) at least five sixths of its `items` produced a
    verdict from Jev itself (`counted`; an outage is a miss, so a Jev that is down half the time
    is not trusted) and (b) its verdicts agree with the hand labels at or above the threshold.
    With `counted` left out, all items are taken as counted."""
    counted = items if counted is None else counted
    needed = max(5, math.ceil(items * c.MIN_COUNTED_SHARE - 1e-9))
    return jev_agreement is not None and counted >= needed and jev_agreement >= threshold


def calibration_estimate(items: Sequence[CalibrationItem], config: Any) -> dict[str, Any]:
    """The estimate of a calibration run: every item goes to the judge model and to Jev."""
    from app.eval.estimate import daily_limits

    need = Need()
    n = len(items)
    need.add("judge", n, n * 1_200)
    need.add("jev", n, n * 1_000)
    limits = daily_limits(config)
    providers = {}
    for who in ("judge", "jev"):
        cap_r, cap_t = limits.requests.get(who), limits.tokens.get(who)
        days = 1
        if cap_r:
            days = max(days, -(-need.requests[who] // int(cap_r * c.DAILY_LIMIT_SAFETY)))
        providers[who] = {
            "label": PROVIDER_LABELS[who],
            "requests": need.requests[who],
            "tokens": need.tokens[who],
            "daily_request_limit": cap_r,
            "daily_token_limit": cap_t,
            "keys": limits.keys.get(who, 0),
            "days_needed": days,
        }
    batches = [[i.id for i in items]]
    return {
        "cases": n,
        "turns": n,
        "judged": True,
        "providers": providers,
        "total_requests": need.total_requests,
        "total_tokens": need.total_tokens,
        "minutes": round(need.total_requests * 0.05, 1),
        "batches": batches,
        "too_big_for_one_day": [],
        "margin": c.OVER_ESTIMATE_MARGIN,
    }


async def run_calibration(
    gateway: Any,
    items: Sequence[CalibrationItem],
    *,
    guard: CostGuard | None = None,
    threshold: float = c.CALIBRATION_THRESHOLD,
) -> dict[str, dict[str, Any]]:
    """Put every item to the judge model alone and to Jev alone; returns per-metric figures. The
    model names are in the figures: a later change of either model makes the figures not count
    (`trusted_metrics`)."""
    from app.eval.judge import VERDICT_TEXT

    config = getattr(gateway, "config", None)
    jev_model = (getattr(config, "models", None) or {}).get("JEV_MODEL")
    judge_model = effective_judge_model(config)
    judge_only = Judge(gateway, guard=guard, trusted={})
    jev_only = Judge(gateway, guard=guard, trusted={metric: True for metric in VERDICT_TEXT})
    verdicts: dict[str, dict[str, list[tuple[bool | None, bool]]]] = defaultdict(
        lambda: {"judge": [], "jev": []}
    )
    for item in items:
        state = {"question": item.question, "passages": {"x": item.context}, "answer": item.answer}
        if item.metric == "claim_supported":
            state = {"claim": item.answer, "passages": {"x": item.context}}
        elif item.metric == "citation_supports":
            state = {"claim": item.answer, "passages": {"x": item.context}}
        elif item.metric == "injection_call_right":
            state = {"message": item.answer, "decision": item.context}
        for name, judge in (("judge", judge_only), ("jev", jev_only)):
            outcome = await judge.verdicts(item.metric, ["x"], state)
            # the decider must be the one asked for: a Jev outage that fell to the judge model
            # does not count as Jev's verdict
            value = outcome.values.get("x")
            if name == "jev" and outcome.decider != "jev":
                value = None
            if name == "judge" and outcome.decider == "jev":
                value = None
            verdicts[item.metric][name].append((value, item.label))
    out: dict[str, dict[str, Any]] = {}
    for metric, parts in verdicts.items():
        judge_agree, judge_n = agreement(parts["judge"])
        jev_agree, jev_n = agreement(parts["jev"])
        # Jev against the judge, on the items both answered: for information only
        both = [
            (j, v)
            for (j, _), (v, _) in zip(parts["judge"], parts["jev"], strict=True)
            if j is not None and v is not None
        ]
        between = sum(1 for j, v in both if j == v) / len(both) if both else None
        out[metric] = {
            "items": len(parts["judge"]),
            "judge_agreement": judge_agree,
            "judge_counted": judge_n,
            "jev_agreement": jev_agree,
            "jev_counted": jev_n,
            "jev_judge_agreement": between,
            "jev_model": jev_model,
            "judge_model": judge_model,
            "threshold": threshold,
            "trusted": trusted(jev_agree, len(parts["jev"]), jev_n, threshold),
        }
    return out


def save(
    db: Database, figures: Mapping[str, Mapping[str, Any]], *, now: datetime | None = None
) -> None:
    for metric, row in figures.items():
        store.save_calibration(
            db,
            metric,
            items=int(row["items"]),
            judge_agreement=row["judge_agreement"],
            jev_agreement=row["jev_agreement"],
            threshold=float(row["threshold"]),
            trusted=bool(row["trusted"]),
            counted=int(row.get("jev_counted") or 0),
            jev_judge_agreement=row.get("jev_judge_agreement"),
            jev_model=row.get("jev_model"),
            judge_model=row.get("judge_model"),
            now=now,
        )


def trusted_metrics(
    db: Database, *, jev_model: str | None = None, judge_model: str | None = None
) -> dict[str, bool]:
    """The metrics Jev may decide, from the stored calibration (empty: none). A row counts only
    when it was made with the Jev model and the judge model that are configured now: a changed
    model has not been calibrated, so its metrics go back to the judge model."""
    out: dict[str, bool] = {}
    for metric, row in store.get_calibration(db).items():
        same = row.get("jev_model") == jev_model and row.get("judge_model") == judge_model
        out[metric] = bool(row["trusted"]) and same
    return out
