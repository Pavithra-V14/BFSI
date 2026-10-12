"""The variance band (spec 15.4): run the active set three times, unchanged; the band is about two
standard deviations. A score inside the band is noise.

Plain statistics, no model. The band is built from the golden scores of the runs of kind
'variance'; a later run's score is compared with it. Alerts only (build rule 5): the verdict is a
word, never a block.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from app.eval import constants as c


def band(scores: Sequence[float], sigmas: float = c.VARIANCE_SIGMAS) -> dict[str, Any]:
    """Mean, sample standard deviation and the noise width (`sigmas` times it). Needs
    `VARIANCE_RUNS` (three) scores: with two, one standard deviation is a guess; with fewer the
    band is undefined (None)."""
    values = [float(s) for s in scores if s is not None and not math.isnan(float(s))]
    if len(values) < c.VARIANCE_RUNS:
        average = (sum(values) / len(values)) if values else None
        return {"n": len(values), "mean": average, "sd": None,
                "width": None, "low": None, "high": None}  # fmt: skip
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    sd = math.sqrt(variance)
    width = sigmas * sd
    return {
        "n": len(values),
        "mean": round(mean, 6),
        "sd": round(sd, 6),
        "width": round(width, 6),
        "low": round(mean - width, 6),
        "high": round(mean + width, 6),
    }


def verdict(score: float | None, baseline: dict[str, Any]) -> str:
    """'noise' inside the band, 'better' above it, 'worse' below it, 'no_band' when the band is
    undefined, 'not_scored' when there is no score."""
    if score is None:
        return "not_scored"
    if baseline.get("width") is None or baseline.get("mean") is None:
        return "no_band"
    if score > baseline["high"]:
        return "better"
    if score < baseline["low"]:
        return "worse"
    return "noise"


def run_golden_scores(db: Any, run_ids: Sequence[str]) -> list[float]:
    """The golden score (mean of `case_pass`) of each run that has one."""
    from app.eval import store

    out: list[float] = []
    for run_id in run_ids:
        scores = [
            r["score"]
            for r in store.get_results(db, run_id)
            if r["metric"] == "case_pass" and r["scored"] and r["score"] is not None
        ]
        if scores:
            out.append(sum(scores) / len(scores))
    return out


def change_in_same_week(
    previous: dict[str, Any] | None, current: dict[str, Any], *, days: int = 7
) -> bool:
    """True when the evaluation set AND the system both changed since the previous run within
    `days` (spec 15.1: "never change the evaluation set and the system in the same week"). An
    alert in the report, not a block."""
    if not previous:
        return False
    try:
        gap = datetime.fromisoformat(str(current["created_at"])) - datetime.fromisoformat(
            str(previous["created_at"])
        )
    except (KeyError, ValueError):
        return False
    if abs(gap) > timedelta(days=days):
        return False
    same_set = previous["dataset_version"] == current["dataset_version"]
    same_system = previous["system_version"] == current["system_version"]
    return (not same_set) and (not same_system)
