"""The Evaluation admin section: two views never mixed, a table beside every chart, run controls
only on the admin machine, a start only with the confirmation code, numbers from the service shown
as plain text."""

import json

from tests.frontend_fixtures import html_bodies, json_response, page_text, signed_in
from tests.test_frontend import admin_api, button, open_section
from tests.test_frontend_hardening import assert_clean

HOSTILE = "see https://evil.example $x$ :warning: a@evil.example"

OVERVIEW = {
    "environment": "local",
    "runs": [
        {
            "id": "r" * 32, "kind": "active", "status": "finished", "finished_items": 20,
            "planned_items": 20, "batch_no": 1, "batches": 1, "requests_used": 120,
            "tokens_used": 40000, "created_at": "2026-10-12T09:00:00+00:00",
        },
    ],
    "series": [
        {"run_id": "r" * 32, "at": "2026-10-11T09:00:00+00:00", "kind": "active",
         "golden_score": 0.7, "dataset_version": "2026-10-v1"},
        {"run_id": "s" * 32, "at": "2026-10-12T09:00:00+00:00", "kind": "active",
         "golden_score": 0.75, "dataset_version": "2026-10-v1"},
    ],
    "band": {"n": 3, "mean": 0.72, "sd": 0.02, "width": 0.04, "low": 0.68, "high": 0.76},
    "latest": {
        "run_id": "s" * 32, "golden_score": 0.75, "verdict": "noise",
        "detail": {
            "run": {"dataset_version": "2026-10-v1"},
            "summary": {
                "by_category": {"factual": 0.9, "injection": 0.5},
                "metrics": {"faithfulness": 0.88, "case_pass": 0.75},
                "counts": {"faithfulness": 18, "case_pass": 20},
                "not_scored": {"faithfulness": 2},
                "stage_latency_ms": {"generate": 1800.5, "rerank": 210.0},
                "model_mix": {"openai/gpt-oss-120b": 18, HOSTILE: 2},
                "reduced_mode_share": 0.1,
                "guardrail_confusion": {"true_positive": 5, "false_negative": 1,
                                        "false_positive": 0, "true_negative": 14},
                "failed_cases": ["j04-repeat-above", "s02-withdrawn-deductible"],
            },
        },
    },
    "set_and_system_changed_together": False,
    "calibration": {
        "claim_supported": {"items": 6, "judge_agreement": 0.9, "jev_agreement": 0.95,
                            "trusted": True},
        "refusal_correct": {"items": 6, "judge_agreement": 0.9, "jev_agreement": 0.5,
                            "trusted": False},
    },
    "drift": [
        {"measures": {"samples": 40, "alerts": ["model_shift"],
                      "measures": {"model": {"psi": 0.4, "js": 0.2, "level": "alert"}}}},
    ],
    "samples": {"total": 12},
    "cases": {"total": 60, "active": 20, "candidates": 2,
              "by_category": {"factual": 8}, "active_by_category": {"factual": 3}},
}  # fmt: skip


def evaluation_api(*, environments=("local",), can_run=False, overview=None):
    api = admin_api()
    api.routes[("GET", "/admin/evaluation/environments")] = json_response(
        200, {"environments": list(environments), "can_run": can_run, "on_render": False}
    )
    api.routes[("GET", "/admin/evaluation/overview")] = json_response(200, overview or OVERVIEW)
    return api


def seen(at):
    """Everything on the page as text: elements, notices, captions and table cells."""
    parts = [page_text(at), *html_bodies(at), *(c.value for c in at.caption)]
    parts += [json.dumps(f.value.to_dict(), default=str) for f in at.dataframe]
    return "\n".join(parts)


def open_evaluation(api, monkeypatch, env=None):
    at = signed_in(api, monkeypatch, role="admin", username="boss", env=env).run()
    return open_section(at, "Evaluation")


# ================================================================================ reading ====
def test_the_quality_numbers_and_the_noise_band_are_shown_with_a_table(monkeypatch):
    at = open_evaluation(evaluation_api(), monkeypatch)
    shown = seen(at)
    assert "75.0 %" in shown and "4.0 %" in shown
    assert "Inside the noise band" in shown
    assert "golden score" in shown and "2026-10-v1" in shown  # the table beside the chart


def test_every_panel_of_the_latest_run_has_its_table(monkeypatch):
    at = open_evaluation(evaluation_api(), monkeypatch)
    shown = seen(at)
    for needle in (
        "factual",
        "injection",
        "faithfulness",
        "generate",
        "rerank",
        "should block, blocked",
        "claim_supported",
        "refusal_correct",
        "model shift",
        "j04-repeat-above",
    ):
        assert needle in shown, needle
    assert "90.0 %" in shown and "not scored" in shown.lower()


def test_calibration_says_which_metrics_jev_may_decide(monkeypatch):
    at = open_evaluation(evaluation_api(), monkeypatch)
    frames = [d for d in at.dataframe]
    flat = json.dumps([f.value.to_dict() for f in frames], default=str)
    assert "Jev may decide" in flat and '"yes"' in flat and '"no"' in flat


def test_text_from_the_service_is_shown_as_plain_text(monkeypatch):
    at = open_evaluation(evaluation_api(), monkeypatch)
    assert not at.exception
    assert_clean(at)


def test_with_no_evaluation_database_the_page_says_what_to_do(monkeypatch):
    api = evaluation_api(environments=())
    at = open_evaluation(api, monkeypatch)
    assert "No evaluation data here yet" in seen(at)
    assert not api.calls("GET", "/admin/evaluation/overview")


def test_the_two_views_are_separate_calls(monkeypatch):
    api = evaluation_api(environments=("local", "production"))
    at = open_evaluation(api, monkeypatch)
    assert api.queries and any(
        p == "/admin/evaluation/overview" and q.get("environment") == "local"
        for p, q in api.queries
    )
    radio = [r for r in at.radio if r.label == "View"][0]
    assert list(radio.options) == ["Local test", "Production"]
    radio.set_value("production").run()
    assert any(
        p == "/admin/evaluation/overview" and q.get("environment") == "production"
        for p, q in api.queries
    )


def test_an_empty_run_history_is_an_honest_nothing(monkeypatch):
    empty = {**OVERVIEW, "runs": [], "series": [], "latest": {"golden_score": None},
             "calibration": {}, "drift": []}  # fmt: skip
    at = open_evaluation(evaluation_api(overview=empty), monkeypatch)
    assert "No finished run yet" in seen(at) and not at.exception


# =============================================================================== running ====
def test_no_run_controls_unless_this_is_the_admin_machine_and_the_view_is_local(monkeypatch):
    api = evaluation_api(can_run=True)
    at = open_evaluation(api, monkeypatch)  # no ADMIN_MACHINE
    assert not [b for b in at.button if "Estimate" in b.label]
    assert "Runs are started on the admin machine only" in seen(at)
    on_machine = open_evaluation(
        evaluation_api(can_run=True), monkeypatch, env={"ADMIN_MACHINE": "true"}
    )
    assert [b for b in on_machine.button if "Estimate" in b.label]
    production = open_evaluation(
        evaluation_api(environments=("production",), can_run=True), monkeypatch,
        env={"ADMIN_MACHINE": "true"},
    )  # fmt: skip
    assert not [b for b in production.button if "Estimate" in b.label]


def test_an_estimate_is_asked_for_then_the_code_is_typed_to_start(monkeypatch):
    api = evaluation_api(can_run=True)
    api.routes[("POST", "/admin/evaluation/estimates")] = json_response(
        200,
        {
            "run_id": "a" * 32,
            "code": "ABCD-1234",
            "lines": ["ignored by the page"],
            "estimate": {
                "cases": 20, "total_requests": 100, "total_tokens": 200000, "minutes": 5.0,
                "margin": 0.25,
                "providers": {
                    "judge": {"label": "judge model", "requests": 60, "tokens": 100000, "keys": 2,
                              "daily_request_limit": 100, "daily_token_limit": None,
                              "days_needed": 1},
                },
            },
            "batches": 2,
        },
    )  # fmt: skip
    api.routes[("POST", f"/admin/evaluation/runs/{'a' * 32}/start")] = json_response(
        202, {"run_id": "a" * 32, "status": "starting"}
    )
    at = open_evaluation(api, monkeypatch, env={"ADMIN_MACHINE": "true"})
    button(at, "Estimate (no call is made)").click().run()
    assert api.calls("POST", "/admin/evaluation/estimates")[0][2] == {
        "set": "active",
        "judged": True,
    }
    shown = seen(at)
    assert "100 requests" in shown and "ABCD-1234" in shown and "judge model" in shown
    assert "2 batch" in shown and "split into resumable batches" in shown and "25 %" in shown
    start = button(at, "Start the run")
    assert start.disabled  # nothing typed yet: no start
    [t for t in at.text_input if t.label == "Confirmation code"][0].set_value("ABCD-1234").run()
    button(at, "Start the run").click().run()
    assert api.calls("POST", f"/admin/evaluation/runs/{'a' * 32}/start")[0][2] == {
        "confirm_code": "ABCD-1234"
    }
    assert "Run started" in seen(at)


def test_a_refused_estimate_is_shown_in_plain_words(monkeypatch):
    from tests.frontend_fixtures import error

    api = evaluation_api(can_run=True)
    api.routes[("POST", "/admin/evaluation/estimates")] = error(
        503, "evaluation_keys_missing", "The evaluation keys are not set on this machine."
    )
    at = open_evaluation(api, monkeypatch, env={"ADMIN_MACHINE": "true"})
    button(at, "Estimate (no call is made)").click().run()
    assert "evaluation keys are not set" in seen(at)


def test_a_wrong_code_is_refused_by_the_service_and_shown(monkeypatch):
    from tests.frontend_fixtures import error

    api = evaluation_api(can_run=True)
    api.routes[("POST", "/admin/evaluation/estimates")] = json_response(
        200, {"run_id": "a" * 32, "code": "ABCD-1234", "lines": ["x"], "estimate": {}, "batches": 1}
    )
    api.routes[("POST", f"/admin/evaluation/runs/{'a' * 32}/start")] = error(
        409, "confirmation_mismatch", "That code does not match this run's estimate."
    )
    at = open_evaluation(api, monkeypatch, env={"ADMIN_MACHINE": "true"})
    button(at, "Estimate (no call is made)").click().run()
    [t for t in at.text_input if t.label == "Confirmation code"][0].set_value("ZZZZ-0000").run()
    button(at, "Start the run").click().run()
    assert "does not match" in seen(at) and "Run started" not in seen(at)
