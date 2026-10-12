"""The Evaluation admin section (spec 15.5, 15.4, 24).

Two views, never mixed: "Local (test)" and "Production". Every chart has a TABLE of the same
numbers (nothing depends on seeing a colour). Only numbers, ids of cases, and fixed words come from
the service; each passes through `render.safe_text` / `admin.cell` like every other API value.

"Run an evaluation" is shown only on the admin machine (ADMIN_MACHINE) and only for the local
view. It takes two steps: an estimate (the service makes no model call; it shows requests and
tokens per provider against the free-tier daily limits, and a confirmation code), then a start that
must carry that code. The page never runs anything itself: the service starts the run as its own
command with the admin machine's own keys.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import render
from frontend.api import ApiClient
from frontend.config import Settings
from frontend.views.admin import _load, _table
from frontend.views.common import friendly, number

VIEW_LABELS = {"local": "Local test", "production": "Production"}
SETS = {"The 20 active cases": "active", "All 60 cases": "full", "Three variance runs": "variance"}
VERDICT_WORDS = {
    "noise": "Inside the noise band: no real change.",
    "better": "Above the noise band: better.",
    "worse": "Below the noise band: worse.",
    "no_band": "No noise band yet: run the active set three times.",
    "not_scored": "Not scored yet.",
}


def _float(value: Any, default: float = 0.0) -> float:
    """A number from an API value; a missing, odd or non-finite one is `default`, never a crash."""
    try:
        number_ = float(value)
    except (TypeError, ValueError):
        return default
    return number_ if number_ == number_ and abs(number_) != float("inf") else default


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f} %"
    except (TypeError, ValueError):
        return "not scored"


def _chart(rows: list[dict[str, Any]], x: str, y: str) -> None:
    clean = [{x: render.safe_text(r.get(x)), y: _float(r.get(y))} for r in rows]
    if clean and any(r[y] for r in clean):
        st.bar_chart(clean, x=x, y=y)


def render_evaluation(client: ApiClient, settings: Settings) -> None:
    render.show(render.who("Evaluation"))
    meta = _load(client.eval_environments())
    if meta is None:
        return
    available = [e for e in meta.get("environments") or [] if e in VIEW_LABELS]
    if not available:
        render.show_notice(
            "info",
            "No evaluation data here yet",
            "No evaluation database is set up for this service. On the admin machine run "
            "`python -m app.eval migrate`; the production view needs EVAL_DATABASE_URL.",
        )
        return
    environment = st.radio(
        "View",
        available,
        format_func=lambda v: render.label_text(VIEW_LABELS[v], 40),
        horizontal=True,
        key="fx_eval_view",
    )
    data = _load(client.eval_overview(environment))
    if data is None:
        return
    render.caption(
        "Local test and Production are never mixed. Every chart has its numbers in a table."
    )
    _quality(data)
    _runs(data)
    _detail(data)
    _calibration(data)
    _drift(data)
    _cases(data)
    if environment == "local" and meta.get("can_run") and settings.admin_machine:
        _run_controls(client)
    elif environment == "local":
        render.caption("Runs are started on the admin machine only.")


def _quality(data: dict[str, Any]) -> None:
    st.subheader("Quality")
    latest = data.get("latest") or {}
    band = data.get("band") or {}
    if latest.get("golden_score") is None:
        st.caption("No finished run yet.")
        return
    left, middle, right = st.columns(3)
    left.metric("Golden-set score", _pct(latest.get("golden_score")))
    middle.metric(
        "Noise band", f"± {_pct(band.get('width'))}" if band.get("width") is not None else "none"
    )
    right.metric(
        "Dataset",
        render.safe_text(((latest.get("detail") or {}).get("run") or {}).get("dataset_version")),
    )
    render.caption(VERDICT_WORDS.get(str(latest.get("verdict")), ""))
    if data.get("set_and_system_changed_together"):
        render.show_notice(
            "info",
            "Set and system changed together",
            "The evaluation set and the system both changed since the previous run: "
            "the comparison is not clean (spec 15.1).",
        )
    series = data.get("series") or []
    _table(
        [
            {
                "run": r.get("run_id"),
                "when": r.get("at"),
                "kind": r.get("kind"),
                "golden score": _pct(r.get("golden_score")),
                "dataset": r.get("dataset_version"),
            }
            for r in series
        ]
    )
    points = [
        {"when": render.safe_text(r.get("at")), "score": _float(r.get("golden_score"))}
        for r in series
        if r.get("golden_score") is not None
    ]
    if len(points) > 1:
        st.line_chart(points, x="when", y="score")


def _runs(data: dict[str, Any]) -> None:
    st.subheader("Runs")
    _table(
        [
            {
                "run": r.get("id"),
                "kind": r.get("kind"),
                "status": r.get("status"),
                "cases": f"{number(r.get('finished_items'))} of {number(r.get('planned_items'))}",
                "batch": f"{number(r.get('batch_no'))} of {number(r.get('batches'))}",
                "requests": r.get("requests_used"),
                "tokens": r.get("tokens_used"),
                "created": r.get("created_at"),
            }
            for r in data.get("runs") or []
        ]
    )


def _detail(data: dict[str, Any]) -> None:
    detail = (data.get("latest") or {}).get("detail")
    if not detail:
        return
    summary = detail.get("summary") or {}
    st.subheader("Latest run in detail")
    categories = summary.get("by_category") or {}
    rows = [{"category": k, "score": _pct(v)} for k, v in sorted(categories.items())]
    _table(rows)
    st.caption("Share of cases that did what was expected, per category.")
    _chart([{"category": r["category"], "score": categories[r["category"]]} for r in rows],
           "category", "score")  # fmt: skip
    metrics = summary.get("metrics") or {}
    st.caption("Metrics (mean over the cases that were scored)")
    _table(
        [
            {
                "metric": name,
                "mean": _pct(value),
                "scored": (summary.get("counts") or {}).get(name),
                "not scored": (summary.get("not_scored") or {}).get(name, 0),
            }
            for name, value in sorted(metrics.items())
        ]
    )
    st.caption("Stage latency (mean milliseconds)")
    stages = summary.get("stage_latency_ms") or {}
    stage_rows = [{"stage": k, "milliseconds": v} for k, v in stages.items()]
    _table(stage_rows)
    _chart(stage_rows, "stage", "milliseconds")
    left, right = st.columns(2)
    with left:
        st.caption("Model mix (cases)")
        _table([{"model": k, "cases": v} for k, v in (summary.get("model_mix") or {}).items()])
        share = summary.get("reduced_mode_share")
        render.caption(f"Reduced-mode answers: {_pct(share)}" if share is not None else "")
    with right:
        st.caption("Guardrails (injection and filter calls)")
        confusion = summary.get("guardrail_confusion") or {}
        _table(
            [
                {"": "should block, blocked", "cases": confusion.get("true_positive")},
                {"": "should block, not blocked", "cases": confusion.get("false_negative")},
                {"": "should not block, blocked", "cases": confusion.get("false_positive")},
                {"": "should not block, not blocked", "cases": confusion.get("true_negative")},
            ]
        )
    failed = summary.get("failed_cases") or []
    if failed:
        st.caption("Cases that did not do what was expected")
        st.code(", ".join(render.safe_text(c) for c in failed), language="text")


def _calibration(data: dict[str, Any]) -> None:
    st.subheader("Calibration")
    rows = data.get("calibration") or {}
    _table(
        [
            {
                "metric": name,
                "items": row.get("items"),
                "judge agreement": _pct(row.get("judge_agreement")),
                "Jev agreement": _pct(row.get("jev_agreement")),
                "Jev may decide": "yes" if row.get("trusted") else "no",
            }
            for name, row in sorted(rows.items())
        ]
    )
    st.caption(
        "Jev decides a metric only when its agreement with the labels reaches the threshold."
    )


def _drift(data: dict[str, Any]) -> None:
    st.subheader("Drift (weekly)")
    snapshots = data.get("drift") or []
    if not snapshots:
        st.caption("No weekly report yet.")
        return
    latest = snapshots[0].get("measures") or {}
    render.caption(f"Samples this week: {number(latest.get('samples'))}")
    _table(
        [
            {
                "signal": name,
                "PSI": row.get("psi"),
                "JS divergence": row.get("js"),
                "level": row.get("level"),
            }
            for name, row in sorted((latest.get("measures") or {}).items())
        ]
    )
    alerts = latest.get("alerts") or []
    if alerts:
        names = ", ".join(render.safe_text(str(a).replace("_", " ")) for a in alerts)
        render.show_notice("info", "Drift signals", names)


def _cases(data: dict[str, Any]) -> None:
    st.subheader("Evaluation set")
    cases = data.get("cases") or {}
    render.caption(
        f"{number(cases.get('total'))} cases, {number(cases.get('active'))} active, "
        f"{number(cases.get('candidates'))} candidate(s) waiting for the next dataset version."
    )
    by = cases.get("by_category") or {}
    active = cases.get("active_by_category") or {}
    _table([{"category": k, "cases": v, "active": active.get(k, 0)} for k, v in sorted(by.items())])


def _estimate_table(estimate: dict[str, Any], batches: Any) -> None:
    """What the run would use, per provider group, against the free-tier daily limits."""
    providers = estimate.get("providers") or {}
    _table(
        [
            {
                "provider": row.get("label"),
                "requests": row.get("requests"),
                "tokens": row.get("tokens"),
                "keys": row.get("keys"),
                "requests a day allowed": row.get("daily_request_limit") or "not published",
                "tokens a day allowed": row.get("daily_token_limit") or "not published",
                "days needed": row.get("days_needed"),
            }
            for row in providers.values()
        ]
    )
    render.caption(
        f"{number(estimate.get('cases'))} cases, {number(estimate.get('total_requests'))} "
        f"requests, {number(estimate.get('total_tokens'))} tokens, about "
        f"{number(estimate.get('minutes'))} minutes, in {number(batches)} batch(es)."
    )
    if number(batches) > 1:
        render.caption(
            "This does not fit one day: it is split into resumable batches, one a day, "
            "each started with the same code."
        )
    margin = _float(estimate.get("margin"), 0.25) * 100
    render.caption(f"The run stops by itself when it uses more than {margin:.0f} % over this.")


def _run_controls(client: ApiClient) -> None:
    st.subheader("Run an evaluation")
    st.caption(
        "Step 1 asks for an estimate: no model call is made. Step 2 starts the run only with the "
        "confirmation code of that estimate. The run uses the evaluation keys only."
    )
    chosen = st.selectbox("Set", list(SETS), key="fx_eval_set")
    judged = st.checkbox("Also score with the judge (uses the evaluation keys)", value=True,
                         key="fx_eval_judged")  # fmt: skip
    if st.button("Estimate (no call is made)", key="fx_eval_estimate"):
        reply = client.eval_estimate(SETS[chosen], judged)
        if reply.ok and isinstance(reply.data, dict):
            st.session_state["fx_eval_pending"] = {
                "run_id": reply.data.get("run_id"),
                "estimate": reply.data.get("estimate") or {},
                "batches": reply.data.get("batches"),
                "code": reply.data.get("code"),
            }
        else:
            render.show_notice("danger", "No estimate", friendly(reply), urgent=True)
    pending = st.session_state.get("fx_eval_pending")
    if not pending:
        return
    _estimate_table(pending["estimate"], pending.get("batches"))
    st.caption("To start this run, type the confirmation code shown here.")
    st.code(render.safe_text(pending["code"]), language="text")
    typed = st.text_input("Confirmation code", key="fx_eval_code", max_chars=16)
    if st.button("Start the run", key="fx_eval_start", disabled=not typed.strip()):
        reply = client.eval_start(str(pending["run_id"]), typed.strip())
        if reply.ok:
            st.session_state.pop("fx_eval_pending", None)
            render.show_notice(
                "ok",
                "Run started",
                "It runs as its own process on this machine. Reload this page to see progress.",
            )
        else:
            render.show_notice("danger", "Not started", friendly(reply), urgent=True)
