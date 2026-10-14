"""The numbers behind the evaluation dashboard (spec 15.5), assembled from one evaluation database.

One environment at a time: the local (test) view and the production view are never mixed. A table
of every number goes with every chart (the frontend draws both from this one payload). Numbers,
names of kinds and case ids only; no answer text.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from app.db.connection import Database
from app.eval import constants as c
from app.eval import metrics, redteam, store, variance


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def run_detail(db: Database, run_id: str) -> dict[str, Any] | None:
    run = store.get_run(db, run_id)
    if run is None:
        return None
    cases = {case.id: case for case in store.list_cases(db, status="set")}
    results = store.get_results(db, run_id)
    items = store.get_items(db, run_id)
    for row in items:
        row["latency_ms"] = row.get("latency_ms")
    summary = metrics.aggregate(results, cases, items)
    stage_totals: dict[str, list[float]] = defaultdict(list)
    models: Counter[str] = Counter()
    degraded = 0
    answered = 0
    for item in items:
        for stage, ms in (item.get("cheap_checks", {}).get("stages_ms") or {}).items():
            stage_totals[stage].append(float(ms))
        for model in item.get("models") or []:
            models[model] += 1
        if item.get("answer_kind") == "answer":
            answered += 1
            if item.get("cheap_checks", {}).get("degraded"):
                degraded += 1
    confusion = guardrail_confusion(items, cases)
    summary["stage_latency_ms"] = {s: _mean(v) for s, v in sorted(stage_totals.items())}
    summary["model_mix"] = dict(sorted(models.items()))
    summary["reduced_mode_share"] = round(degraded / answered, 4) if answered else None
    summary["guardrail_confusion"] = confusion
    per_case = defaultdict(dict)
    for row in results:
        per_case[row["case_id"]][row["metric"]] = row["score"]
    summary["failed_cases"] = sorted(
        cid for cid, scores in per_case.items() if scores.get("case_pass") == 0.0
    )
    return {"run": _public_run(run), "summary": summary}


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    """The run row as the dashboard shows it: no creator name beyond a short label."""
    keep = (
        "id", "environment", "kind", "dataset_version", "status", "planned_items",
        "finished_items", "batch_no", "batches", "tokens_used", "requests_used", "created_at",
        "started_at", "finished_at", "note", "confirmed_at",
    )  # fmt: skip
    data = {k: run[k] for k in keep}
    data["system_version"] = run["system_version"]
    data["estimate"] = {
        "total_requests": run["estimate"].get("total_requests"),
        "total_tokens": run["estimate"].get("total_tokens"),
        "minutes": run["estimate"].get("minutes"),
        "cases": run["estimate"].get("cases"),
    }
    return data


def guardrail_confusion(items: list[dict[str, Any]], cases: dict[str, Any]) -> dict[str, int]:
    """Injection and filter calls as a confusion matrix: a message that SHOULD be blocked is
    positive; blocked means the system refused with the reason `blocked`."""
    tp = fn = fp = tn = 0
    for item in items:
        case = cases.get(item["case_id"])
        if case is None or item.get("status") != "done":
            continue
        blocked = item.get("answer_kind") == "reword" or (
            item.get("answer_kind") == "refusal" and item.get("reason") == "blocked"
        )
        should = case.behaviour == "block"
        if should and blocked:
            tp += 1
        elif should:
            fn += 1
        elif blocked:
            fp += 1
        else:
            tn += 1
    return {"true_positive": tp, "false_negative": fn, "false_positive": fp, "true_negative": tn}


def overview(db: Database, environment: str) -> dict[str, Any]:
    runs = store.list_runs(db, environment=environment, limit=30)
    finished = [r for r in runs if r["status"] == c.RUN_FINISHED]
    quality = [
        r
        for r in finished
        if r["kind"] in ("active", "full", "variance")
        and not redteam.is_redteam_version(r["dataset_version"])
    ]  # runs that have a golden score; the red-team set is judged apart
    series = []
    for run in reversed(quality):
        golden = variance.run_golden_scores(db, [run["id"]])
        series.append(
            {
                "run_id": run["id"],
                "at": run["finished_at"] or run["created_at"],
                "kind": run["kind"],
                "golden_score": round(golden[0], 4) if golden else None,
                "dataset_version": run["dataset_version"],
            }
        )
    variance_runs = [r["id"] for r in finished if r["kind"] == "variance"][:3]
    band = variance.band(variance.run_golden_scores(db, variance_runs))
    latest = quality[0] if quality else None
    latest_score = None
    detail = None
    if latest is not None:
        scores = variance.run_golden_scores(db, [latest["id"]])
        latest_score = scores[0] if scores else None
        detail = run_detail(db, latest["id"])
    previous = quality[1] if len(quality) > 1 else None
    changed_together = bool(latest and previous and variance.change_in_same_week(previous, latest))
    return {
        "environment": environment,
        "runs": [_public_run(r) for r in runs],
        "series": series,
        "band": band,
        "latest": {
            "run_id": latest["id"] if latest else None,
            "golden_score": round(latest_score, 4) if latest_score is not None else None,
            "verdict": variance.verdict(latest_score, band),
            "detail": detail,
        },
        "set_and_system_changed_together": changed_together,
        "calibration": store.get_calibration(db),
        "drift": store.list_drift(db, environment, limit=8),
        "samples": {"total": store.count_samples(db)},
        "cases": cases_overview(db),
    }


def cases_overview(db: Database) -> dict[str, Any]:
    cases = store.list_cases(db, status="set")
    candidates = store.list_cases(db, status="candidate")
    by_category: Counter[str] = Counter(case.category for case in cases)
    active: Counter[str] = Counter(case.category for case in cases if case.active)
    return {
        "total": len(cases),
        "active": sum(active.values()),
        "by_category": dict(sorted(by_category.items())),
        "active_by_category": dict(sorted(active.items())),
        "candidates": len(candidates),
        "dataset_versions": store.dataset_versions(db),
    }
