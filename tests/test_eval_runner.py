"""The harness, the scenarios and a confirmed run (Phase 13), over the stubbed corpus.

Everything is local: an in-memory test collection named `documents_eval`, a scratch SQLite file,
a scripted model. No network, no key. Test names say the rule each one guards."""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from qdrant_client import models

from app.db.migrate import run as migrate
from app.eval import constants as c
from app.eval import metrics, store
from app.eval.cases import Op
from app.eval.guard import CostGuard, NotConfirmed
from app.eval.runner import (
    RunExecutor,
    current_batch,
    guard_for,
    prepare_run,
    start_run,
)
from app.eval.scenarios import Scenario, ScenarioError
from app.gateway.config import GatewayConfig
from tests.eval_rig import COLLECTION, EvalRig

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
MF, INS, BANK = "mf-ecas-servicing", "insurance-flexi-property", "banking-rtl-facility"


def config(**overrides):
    values = {
        "groq_keys": ["g" * 12],
        "gemini_keys": ["m" * 12],
        "eval_openrouter_keys": ["j" * 12, "k" * 12, "l" * 12, "n" * 12],
        "jev_key": "v" * 12,
        "embedding_keys": ["e" * 12],
        "cohere_key": "c" * 12,
    }
    values.update(overrides)
    return GatewayConfig(**values)


@pytest.fixture
def rig(tmp_path):
    made = EvalRig(tmp_path)
    yield made
    made.close()


@pytest.fixture
def eval_db(tmp_path):
    url = store.sqlite_url(tmp_path / "eval.db")
    migrate(url, store.MIGRATIONS, log=lambda line: None)
    db = store.open_db(url)
    yield db
    db.close()


def apply(scenario, op):
    asyncio.run(scenario.apply(op))


def run_case(rig, case_id):
    case = rig.case(case_id)
    return case, asyncio.run(rig.harness.run_case(case))


def points_of(rig, doc_id):
    flt = models.Filter(
        must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]
    )
    found, _ = rig.qdrant.scroll(COLLECTION, scroll_filter=flt, limit=100, with_payload=True)
    return found


# ================================================================================ harness ====
def test_a_customer_factual_case_is_answered_from_its_clause(rig):
    case, item = run_case(rig, "f04-stamp-duty")
    assert item["answer_kind"] == "answer" and "0.005 per cent" in item["answer_text"]
    scores = {s.metric: s.value for s in metrics.score_item(case, item)}
    assert scores["behaviour_correct"] == scores["fact_recall"] == scores["source_hit"] == 1.0
    assert scores["case_pass"] == 1.0


def test_the_item_holds_what_a_judge_needs_and_names_no_user(rig):
    _case, item = run_case(rig, "f04-stamp-duty")
    assert item["contexts"] and len(item["chunk_ids"]) == len(item["contexts"])
    assert len(item["clause_refs"]) == len(item["contexts"]) and item["doc_ids"]
    assert item["cheap_checks"]["cited_idx"], "the cited passage is recorded by position"
    assert item["models"] and item["tokens"] > 0 and item["latency_ms"] > 0
    shown = json.dumps(item)
    assert "eval_customer" not in shown and "@eval.invalid" not in shown


def test_usage_is_counted_by_provider_group(rig):
    _case, item = run_case(rig, "f04-stamp-duty")
    usage = item["usage"]
    assert usage["answer"][0] >= 1 and usage["answer"][1] > 0
    assert set(usage) <= {"answer", "jev", "embedding", "rerank"}


def test_a_multi_turn_case_uses_one_session_and_answers_the_last_turn(rig):
    case, item = run_case(rig, "m01-stt")
    assert len(case.turns) == 2 and item["answer_kind"] == "answer"
    rows = rig.db.query_all("SELECT COUNT(DISTINCT session_id) FROM messages")
    assert rows[0][0] >= 1
    sessions = rig.db.query_all("SELECT id FROM sessions")
    assert len(sessions) == 1  # two turns, one conversation


def test_the_run_uses_its_own_users_per_role_and_never_a_real_account(rig):
    run_case(rig, "f04-stamp-duty")
    run_case(rig, "f01-penal-charge")
    names = {r[0] for r in rig.db.query_all("SELECT username FROM users")}
    assert names == {"eval_customer", "eval_staff"}


def test_a_resumed_run_reuses_the_users_the_scratch_file_already_holds(rig):
    first = rig.harness.user_for("customer")
    rig.harness._users.clear()  # a new process on the next day: same scratch file, empty memory
    assert rig.harness.user_for("customer") == first
    assert rig.db.query_one("SELECT COUNT(*) FROM users")[0] == 1


def test_a_customer_cannot_be_answered_from_the_staff_only_document(rig):
    case, item = run_case(rig, "r01-customer-spread")
    assert "1.85" not in item["answer_text"]
    assert BANK not in item["doc_ids"]
    scores = {s.metric: s.value for s in metrics.score_item(case, item)}
    assert scores["forbidden_absent"] == 1.0


def test_a_staff_member_is_answered_from_the_staff_only_document(rig):
    case, item = run_case(rig, "r02-staff-spread")
    assert BANK in item["doc_ids"] and "1.85" in item["answer_text"]


def test_the_harness_refuses_the_production_collection(rig):
    from app.eval.runner import Harness

    with pytest.raises(c.EvalConfigError):
        Harness(rig.pipeline, rig.db, rig.qdrant, "documents", rig.tmp_path)


# ============================================================================== scenarios ====
def snapshot_of(rig):
    return rig.snapshot()


def test_a_withdrawn_document_is_inactive_and_comes_back(rig):
    before = snapshot_of(rig)
    scenario = Scenario(rig.qdrant, COLLECTION, rig.tmp_path / "j.json")
    apply(scenario, Op("withdraw_document", MF))
    assert all(p.payload["effective_to"] for p in points_of(rig, MF))
    assert all(not p.payload["effective_to"] for p in points_of(rig, INS))  # only that document
    assert scenario.restore_all() == 1
    assert snapshot_of(rig) == before and scenario.pending == 0


def test_an_edit_changes_the_text_and_the_lexical_vector_and_is_undone(rig):
    before = snapshot_of(rig)
    scenario = Scenario(rig.qdrant, COLLECTION, rig.tmp_path / "j.json")
    apply(scenario, Op("edit_text", MF, anchor="Stamp duty of 0.005", old="0.005", new="0.010"))
    texts = [p.payload["text"] for p in points_of(rig, MF)]
    assert any("0.010 per cent" in t for t in texts) and not any(
        "0.005 per cent" in t for t in texts
    )
    scenario.restore_all()
    assert snapshot_of(rig) == before


def test_a_superseding_version_adds_a_newer_point_and_retires_the_old_one(rig):
    before = snapshot_of(rig)
    scenario = Scenario(rig.qdrant, COLLECTION, rig.tmp_path / "j.json")
    apply(
        scenario,
        Op("supersede_text", MF, anchor="taxed at 12.5 per cent", old="12.5", new="13.0"),
    )
    points = points_of(rig, MF)
    active = [p for p in points if not p.payload["effective_to"]]
    retired = [p for p in points if p.payload["effective_to"]]
    assert any("13.0 per cent" in p.payload["text"] for p in active)
    assert any("12.5 per cent" in p.payload["text"] for p in retired)
    assert not any("12.5 per cent" in p.payload["text"] for p in active)
    assert any(p.payload["doc_version"] == 2 for p in active)
    scenario.restore_all()
    assert snapshot_of(rig) == before


def test_a_case_that_fails_still_undoes_its_changes(rig, monkeypatch):
    before = snapshot_of(rig)
    case = rig.case("e01-edit-stamp-duty")

    async def boom(request, db, **kwargs):
        raise RuntimeError("the pipeline broke")

    monkeypatch.setattr(rig.pipeline, "answer", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(rig.harness.run_case(case))
    assert snapshot_of(rig) == before


def test_changes_left_by_a_crash_are_restored_from_the_journal(rig):
    before = snapshot_of(rig)
    journal = rig.tmp_path / "crash.json"
    first = Scenario(rig.qdrant, COLLECTION, journal)
    apply(first, Op("withdraw_document", INS))
    apply(first, Op("edit_text", MF, anchor="Stamp duty of 0.005", old="0.005", new="0.010"))
    assert journal.is_file() and snapshot_of(rig) != before
    # the process "dies": a new one finds the journal and puts everything back
    second = Scenario(rig.qdrant, COLLECTION, journal)
    assert second.pending == 2
    assert second.restore_all() == 2
    assert snapshot_of(rig) == before and not journal.exists()


def test_a_scenario_refuses_the_production_collection(rig):
    with pytest.raises(c.EvalConfigError):
        Scenario(rig.qdrant, "documents", rig.tmp_path / "j.json")


def test_a_scenario_names_what_it_cannot_find(rig):
    scenario = Scenario(rig.qdrant, COLLECTION, rig.tmp_path / "j.json")
    with pytest.raises(ScenarioError):
        apply(scenario, Op("withdraw_document", "no-such-document"))
    with pytest.raises(ScenarioError):
        apply(scenario, Op("edit_text", MF, anchor="a phrase that is not there", old="a", new="b"))


def test_the_edit_case_sees_the_new_text_and_the_collection_is_whole_after(rig):
    before = snapshot_of(rig)
    case, item = run_case(rig, "e01-edit-stamp-duty")
    assert any("0.010 per cent" in text for text in item["contexts"])
    assert "0.010 per cent" in item["answer_text"] and "0.005" not in item["answer_text"]
    assert snapshot_of(rig) == before


def test_the_withdrawn_follow_up_loses_its_source(rig):
    before = snapshot_of(rig)
    case, item = run_case(rig, "s01-withdrawn-follow-up")
    assert "Securities Transaction Tax" not in item["answer_text"]
    assert MF not in item["doc_ids"]
    assert snapshot_of(rig) == before


# ============================================================================== a run ====
def prepared(rig, eval_db, case_ids, **kwargs):
    store.sync_cases(eval_db, rig.cases)
    cases = [x for x in rig.cases if x.id in case_ids]
    run_id, estimate, code = prepare_run(
        eval_db, cases=cases, kind="active", environment="local",
        dataset_version=rig.version, config=config(), created_by="test", judged=False, now=NOW,
        **kwargs,
    )  # fmt: skip
    return run_id, estimate, code


def executor(rig, eval_db, run_id, *, estimate_scale=1.0):
    run = store.get_run(eval_db, run_id)
    guard = guard_for(run)
    if estimate_scale != 1.0:
        guard = CostGuard(
            int(run["estimate"]["total_requests"] * estimate_scale),
            int(run["estimate"]["total_tokens"] * estimate_scale),
        )
    cases = {x.id: x for x in rig.cases}
    return RunExecutor(eval_db, rig.harness, guard, None, cases, run_id, now=lambda: NOW), run


def test_a_run_does_not_start_without_the_confirmation_code(rig, eval_db):
    run_id, _estimate, code = prepared(rig, eval_db, ["f04-stamp-duty"])
    assert store.get_run(eval_db, run_id)["status"] == c.RUN_ESTIMATED
    calls_before = len(rig.gateway.calls)
    for wrong in (None, "", "XXXX-0000", code[:-1]):
        with pytest.raises(NotConfirmed):
            start_run(eval_db, run_id, wrong, now=NOW)
    assert store.get_run(eval_db, run_id)["status"] == c.RUN_ESTIMATED
    assert len(rig.gateway.calls) == calls_before == 0  # preparing and refusing call nothing


def test_the_right_code_marks_the_run_confirmed_and_running(rig, eval_db):
    run_id, _estimate, code = prepared(rig, eval_db, ["f04-stamp-duty"])
    start_run(eval_db, run_id, code, now=NOW)
    run = store.get_run(eval_db, run_id)
    assert run["status"] == c.RUN_RUNNING and run["confirmed_at"] is not None


def test_a_confirmed_run_saves_each_case_and_its_scores(rig, eval_db):
    ids = ["f04-stamp-duty", "f01-penal-charge", "g01-hi"]
    run_id, _estimate, code = prepared(rig, eval_db, ids)
    start_run(eval_db, run_id, code, now=NOW)
    ex, run = executor(rig, eval_db, run_id)
    outcome = asyncio.run(ex.run_batch(current_batch(run)))
    assert outcome.status == c.RUN_FINISHED and outcome.finished_items == 3
    items = {i["case_id"]: i for i in store.get_items(eval_db, run_id)}
    assert items["f04-stamp-duty"]["answer_kind"] == "answer"
    assert items["g01-hi"]["answer_kind"] == "reply"
    results = store.get_results(eval_db, run_id)
    assert {"case_pass", "behaviour_correct"} <= {r["metric"] for r in results}
    summary = metrics.aggregate(results, {x.id: x for x in rig.cases})
    assert summary["golden_score"] == 1.0
    final = store.get_run(eval_db, run_id)
    assert final["finished_items"] == 3 and final["tokens_used"] > 0 and final["finished_at"]


def test_a_stopped_run_resumes_without_asking_the_finished_cases_again(rig, eval_db):
    ids = ["f04-stamp-duty", "f01-penal-charge", "t01-spread-cr4"]
    run_id, _estimate, code = prepared(rig, eval_db, ids)
    start_run(eval_db, run_id, code, now=NOW)
    ex, run = executor(rig, eval_db, run_id)
    first_two = current_batch(run)[:2]
    asyncio.run(ex.run_batch(first_two))  # a stall after two cases
    done = [i["case_id"] for i in store.get_items(eval_db, run_id) if i["status"] == "done"]
    assert sorted(done) == sorted(first_two)
    asked = len([c_ for c_ in rig.gateway.calls if c_["kind"] == "complete"])
    ex2, run = executor(rig, eval_db, run_id)
    outcome = asyncio.run(ex2.run_batch(current_batch(run)))
    assert outcome.status == c.RUN_FINISHED
    new_asked = len([c_ for c_ in rig.gateway.calls if c_["kind"] == "complete"]) - asked
    assert new_asked == 1, "only the one remaining case was asked"


def test_a_run_that_goes_over_its_estimate_stops_and_leaves_the_collection_whole(rig, eval_db):
    ids = ["e01-edit-stamp-duty", "f04-stamp-duty", "f01-penal-charge", "t01-spread-cr4"]
    run_id, _estimate, code = prepared(rig, eval_db, ids)
    start_run(eval_db, run_id, code, now=NOW)
    before = rig.snapshot()
    ex, run = executor(rig, eval_db, run_id, estimate_scale=0.05)  # an estimate far too low
    outcome = asyncio.run(ex.run_batch(current_batch(run)))
    assert outcome.status == c.RUN_ABORTED
    stored = store.get_run(eval_db, run_id)
    assert stored["status"] == c.RUN_ABORTED and "over the estimate" in (stored["note"] or "")
    assert rig.snapshot() == before
    with pytest.raises(c.EvalConfigError):
        start_run(eval_db, run_id, code, now=NOW)  # an aborted run needs a new estimate


def test_the_over_estimate_stop_comes_before_the_next_case_is_asked(rig, eval_db):
    ids = ["f04-stamp-duty", "f01-penal-charge", "t01-spread-cr4", "t02-cover-cr5"]
    run_id, _estimate, code = prepared(rig, eval_db, ids)
    start_run(eval_db, run_id, code, now=NOW)
    ex, run = executor(rig, eval_db, run_id, estimate_scale=0.01)
    asyncio.run(ex.run_batch(current_batch(run)))
    done = [i for i in store.get_items(eval_db, run_id) if i["status"] == "done"]
    assert len(done) <= 1  # the first case already passed the limit: no second one was started


def test_a_run_that_does_not_fit_a_day_pauses_after_a_batch_and_resumes(rig, eval_db):
    store.sync_cases(eval_db, rig.cases)
    cases = [
        x for x in rig.cases if x.id in ("f04-stamp-duty", "f01-penal-charge", "t01-spread-cr4")
    ]
    tight = config(eval_openrouter_keys=["j" * 12])  # 50 judge requests a day
    run_id, estimate, code = prepare_run(
        eval_db, cases=cases, kind="active", environment="local",
        dataset_version=rig.version, config=tight, created_by="test", judged=True, now=NOW,
    )  # fmt: skip
    assert len(estimate["batches"]) >= 2
    start_run(eval_db, run_id, code, now=NOW)
    ex, run = executor(rig, eval_db, run_id)
    first = asyncio.run(ex.run_batch(current_batch(run)))
    assert first.status == c.RUN_PAUSED
    paused = store.get_run(eval_db, run_id)
    assert paused["batch_no"] == 2 and paused["status"] == c.RUN_PAUSED
    start_run(eval_db, run_id, code, now=NOW)  # the next day's resume uses the same code
    ex2, run = executor(rig, eval_db, run_id)
    second = asyncio.run(ex2.run_batch(current_batch(run)))
    assert second.status in (c.RUN_PAUSED, c.RUN_FINISHED)


def test_usage_is_written_per_day_and_group(rig, eval_db):
    run_id, _estimate, code = prepared(rig, eval_db, ["f04-stamp-duty"])
    start_run(eval_db, run_id, code, now=NOW)
    ex, run = executor(rig, eval_db, run_id)
    asyncio.run(ex.run_batch(current_batch(run)))
    used = store.usage_for_day(eval_db, NOW.strftime("%Y-%m-%d"))
    assert used["answer"][0] >= 1 and used["answer"][1] > 0


# ============================================================ the stubbed core set (CI job) ====
def test_the_active_set_runs_end_to_end_on_the_stubbed_model(rig, eval_db):
    """The alert-only CI job (spec 17.2) runs this file: a stubbed model, no key, no network.
    It proves the harness, the scenarios, the scoring and the run bookkeeping work together;
    the quality of a real model is measured only by an owner-run evaluation."""
    store.sync_cases(eval_db, rig.cases)
    active = [x for x in rig.cases if x.active]
    run_id, _estimate, code = prepare_run(
        eval_db, cases=active, kind="active", environment="local",
        dataset_version=rig.version, config=config(), created_by="ci", judged=False, now=NOW,
    )  # fmt: skip
    start_run(eval_db, run_id, code, now=NOW)
    before = rig.snapshot()
    totals = []
    for _ in range(10):  # one batch a day; a judged run would span days, this one is unjudged
        ex, run = executor(rig, eval_db, run_id)
        outcome = asyncio.run(ex.run_batch(current_batch(run)))
        totals.append(outcome.status)
        if outcome.status == c.RUN_FINISHED:
            break
        start_run(eval_db, run_id, code, now=NOW)
    assert totals[-1] == c.RUN_FINISHED
    items = store.get_items(eval_db, run_id)
    assert len(items) == len(active) and all(i["status"] == "done" for i in items)
    summary = metrics.aggregate(
        store.get_results(eval_db, run_id), {x.id: x for x in rig.cases}, items
    )
    assert summary["golden_score"] is not None and summary["golden_score"] >= 0.9
    assert set(summary["by_category"]) == {x.category for x in active}
    assert rig.snapshot() == before  # every edit, withdrawal and new version was undone
    print(f"eval core set (stubbed model): golden score {summary['golden_score']}")
