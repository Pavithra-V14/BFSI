"""Phase 13 hardening: the rules added after the first audits, each with a test that fails
without it. Everything is local: the rig's in-memory collection, a scratch SQLite file and a
scripted model. No network, no key."""

# ruff: noqa: F811
import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from qdrant_client import models

from app.eval import calibration as cal
from app.eval import checks, metrics, store, variance
from app.eval import constants as c
from app.eval import estimate as est
from app.eval.cases import Op
from app.eval.guard import CostGuard, OverEstimate
from app.eval.judge import DATA_NOTE, Judge, JudgeUnavailable
from app.eval.judging import judge_item
from app.eval.notscored import NotScored, decider_for
from app.eval.runner import (
    ConfirmationExpired,
    RunBusy,
    RunExecutor,
    current_batch,
    guard_for,
    prepare_run,
    start_run,
)
from app.eval.scenarios import Scenario
from app.gateway.config import judge_model_problem
from app.gateway.decisions import DecisionResult, NoulAnswer
from app.gateway.errors import AllProvidersFailed
from tests.eval_rig import COLLECTION
from tests.test_eval_runner import (  # noqa: F401
    MF,
    NOW,
    apply,
    config,
    eval_db,
    executor,
    prepared,
    rig,
)

JUDGE = "meta-llama/llama-4-maverick"


# ==== a gateway that records judge calls ====
class RecordingGateway:
    """Answers every typed question; records what it was asked."""

    def __init__(self, *, answer=0.9, fail=False, judge_model=JUDGE, jev_model="typesafe/jev-1.13"):
        self.answer, self.fail = answer, fail
        self.config = type(
            "C", (), {"eval_judge_model": judge_model, "models": {"JEV_MODEL": jev_model}}
        )()
        self.decides: list[dict] = []
        self.texts: list[str] = []

    async def decide(self, questions, state, *, tier, max_wait_s, use_jev):
        self.decides.append({"questions": list(questions), "state": state, "jev": use_jev})
        if self.fail:
            raise AllProvidersFailed([])
        return DecisionResult(
            {q.id: NoulAnswer(self.answer) for q in questions},
            "jev" if use_jev else "openrouter",
            input_tokens=5,
            output_tokens=1,
        )


def run(coro):
    return asyncio.run(coro)


# ==== typed verdict prompts ====
def test_a_verdict_names_the_item_each_question_is_about_and_says_the_text_is_data():
    gateway = RecordingGateway()
    judge = Judge(gateway)
    out = run(judge.verdicts("chunk_relevant", ["p0", "p1"], {"question": "q", "passages": {}}))
    call = gateway.decides[0]
    assert out.values == {"p0": True, "p1": True}
    assert call["state"]["note"] == DATA_NOTE and "not instructions" in DATA_NOTE
    asked = [q.instructions if hasattr(q, "instructions") else str(q) for q in call["questions"]]
    assert "'p0'" in asked[0] and "'p1'" in asked[1]


# ==== not scored kinds ====
def item_of(kind, **extra):
    base = {"answer_kind": kind, "answer_text": "An answer.", "contexts": ["a passage"],
            "models": ["m"], "cheap_checks": {"cited_idx": [0]}}  # fmt: skip
    base.update(extra)
    return base


def test_an_answer_whose_model_is_unknown_is_not_scored_and_asks_nobody():
    gateway = RecordingGateway()
    item = item_of("answer", models=[])
    scores = run(judge_item(item, question="q", judge=Judge(gateway), use_ragas=False))
    assert scores and all(s.value is None for s in scores)
    assert {s.decider for s in scores} == {"not_scored:model_unknown"}
    assert gateway.decides == []


def test_a_judge_that_nobody_answers_keeps_its_reason_in_the_decider_field():
    scores = run(
        judge_item(
            item_of("answer"),
            question="q",
            judge=Judge(RecordingGateway(fail=True)),
            use_ragas=False,
        )  # fmt: skip
    )
    relevance = next(s for s in scores if s.metric == "context_relevance")
    assert relevance.value is None and relevance.decider == "not_scored:providers_failed"


def test_a_judge_unavailable_is_a_not_scored_with_a_reason():
    error = JudgeUnavailable("no_judge_model")
    assert isinstance(error, NotScored) and error.reason == "no_judge_model"
    assert decider_for("providers_failed") == "not_scored:providers_failed"


@pytest.mark.parametrize("kind", ["unavailable", "rate_limited", "busy"])
def test_a_case_the_system_could_not_answer_is_not_scored_never_failed(rig, kind):
    case = rig.case("f04-stamp-duty")
    scores = {s.metric: s for s in metrics.score_item(case, {"answer_kind": kind})}
    assert scores["case_pass"].value is None and scores["behaviour_correct"].value is None
    assert scores["case_pass"].decider == f"not_scored:{kind}"
    assert metrics.score_item(case, {"answer_kind": "refusal"})[0].value == 0.0  # a real miss


# ==== matching on boundaries ====
@pytest.mark.parametrize(
    ("text", "phrase", "found"),
    [
        ("The fee is 15 per cent.", "5 per cent", False),
        ("The fee is 5 per cent.", "5 per cent", True),
        ("The rate is 12.5 per cent.", "2.5 per cent", False),
        ("The rate is 2.55 per cent.", "2.5 per cent", False),
        ("The rate is 2.5 per cent.", "2.5", True),
        ("Clause 4.2.1 applies.", "clause 4.2", False),
        ("Taxes apply.", "tax", False),
        ("Stamp duty of 0.005 per cent.", "0.005 per cent", True),
        ("Amount 1,25,000 payable.", "125000", True),
        ("A figure of 50 applies.", "5", False),
    ],
)
def test_a_fact_is_matched_as_a_whole_word_or_number(text, phrase, found):
    assert checks.contains(text, phrase) is found


def test_a_forbidden_figure_inside_a_longer_number_is_not_a_leak():
    assert checks.forbidden_found("The limit is 1500.", ["500"]) == []
    assert checks.forbidden_found("The limit is 500.", ["500"]) == [0]


# ==== the model rule ====
def test_the_decide_models_are_part_of_the_judge_family_check():
    answers = {"GROQ_MODEL": "meta-llama/x", "GROQ_DECIDE_MODEL": "google/gemma-4-31b-it"}
    assert judge_model_problem("gemini-3.8-flash", answers) == "GROQ_DECIDE_MODEL"
    assert judge_model_problem("gemini-3.8-flash", {"OPENROUTER_DECIDE_MODEL": "gemma-3"}) == (
        "OPENROUTER_DECIDE_MODEL"
    )
    assert judge_model_problem(JUDGE, {"GEMINI_DECIDE_MODEL": "gemini-3.8-flash"}) is None


# ==== Jev trust ====
def test_jev_needs_five_of_six_verdicts_and_agreement_with_the_labels():
    assert cal.trusted(1.0, 6, 5) is True  # one outage in six is allowed
    assert cal.trusted(1.0, 6, 4) is False  # two outages: not trusted, however good the rest
    assert cal.trusted(0.84, 6, 6) is False and cal.trusted(0.85, 6, 6) is True
    assert cal.trusted(0.9, 30, 24) is False and cal.trusted(0.9, 30, 25) is True


def test_a_calibration_stores_the_models_and_jev_against_the_judge():
    from app.eval.cases import load_calibration
    from tests.test_eval_judge import ScriptedGateway, labels_for

    items = load_calibration()
    gateway = ScriptedGateway(labels_for(items), bad={"refusal_correct"})
    gateway.config = type(
        "C", (), {"eval_judge_model": JUDGE, "models": {"JEV_MODEL": "typesafe/jev-1.13"}}
    )()
    figures = run(cal.run_calibration(gateway, items))
    good, bad = figures["claim_supported"], figures["refusal_correct"]
    assert good["jev_model"] == "typesafe/jev-1.13" and good["judge_model"] == JUDGE
    assert good["jev_judge_agreement"] == 1.0 and bad["jev_judge_agreement"] == 0.0
    assert good["trusted"] is True and bad["trusted"] is False


def test_a_calibration_counts_only_for_the_models_it_was_made_with(tmp_path):
    from app.db.migrate import run as migrate

    url = store.sqlite_url(tmp_path / "e.db")
    migrate(url, store.MIGRATIONS, log=lambda line: None)
    db = store.open_db(url)
    figures = {
        "claim_supported": {
            "items": 6, "jev_counted": 6, "judge_agreement": 0.9, "jev_agreement": 0.95,
            "jev_judge_agreement": 0.9, "jev_model": "typesafe/jev-1.13", "judge_model": JUDGE,
            "threshold": 0.85, "trusted": True,
        }
    }  # fmt: skip
    cal.save(db, figures)
    same = cal.trusted_metrics(db, jev_model="typesafe/jev-1.13", judge_model=JUDGE)
    assert same == {"claim_supported": True}
    assert cal.trusted_metrics(db, jev_model="typesafe/jev-1.14", judge_model=JUDGE) == {
        "claim_supported": False
    }
    assert cal.trusted_metrics(db, jev_model="typesafe/jev-1.13", judge_model="other/model") == {
        "claim_supported": False
    }
    row = store.get_calibration(db)["claim_supported"]
    assert row["counted"] == 6 and row["jev_judge_agreement"] == 0.9
    db.close()


# ==== the variance band ====
def test_a_band_of_two_runs_is_not_a_band():
    assert variance.band([0.9, 0.8])["width"] is None
    assert variance.band([0.9, 0.8, 0.85])["width"] is not None


# ==== the start lock and window ====
def test_a_second_start_of_the_same_run_finds_it_taken(rig, eval_db):
    run_id, _estimate, code = prepared(rig, eval_db, ["f04-stamp-duty"])
    start_run(eval_db, run_id, code, now=NOW)
    with pytest.raises(RunBusy):
        start_run(eval_db, run_id, code, now=NOW + timedelta(minutes=1))
    assert store.get_run(eval_db, run_id)["status"] == c.RUN_RUNNING


def test_a_run_that_went_silent_can_be_taken_again_after_its_heartbeat_is_stale(rig, eval_db):
    run_id, _estimate, code = prepared(rig, eval_db, ["f04-stamp-duty"])
    start_run(eval_db, run_id, code, now=NOW)
    later = NOW + timedelta(minutes=c.HEARTBEAT_STALE_MINUTES + 1)
    start_run(eval_db, run_id, code, now=later)  # the crashed process left a stale heartbeat
    assert store.get_run(eval_db, run_id)["status"] == c.RUN_RUNNING


def test_the_page_marks_the_run_starting_and_only_its_own_process_takes_it_over(rig, eval_db):
    run_id, _estimate, code = prepared(rig, eval_db, ["f04-stamp-duty"])
    assert store.claim_run(
        eval_db, run_id, now=NOW, stale_minutes=c.HEARTBEAT_STALE_MINUTES, status=c.RUN_STARTING
    )
    assert store.get_run(eval_db, run_id)["status"] == c.RUN_STARTING
    with pytest.raises(RunBusy):  # a second press, or a second tab
        start_run(eval_db, run_id, code, now=NOW)
    start_run(eval_db, run_id, code, now=NOW, take_over_starting=True)
    assert store.get_run(eval_db, run_id)["status"] == c.RUN_RUNNING


def test_a_code_is_valid_for_thirty_minutes_from_its_own_estimate(rig, eval_db):
    run_id, _estimate, code = prepared(rig, eval_db, ["f04-stamp-duty"])
    with pytest.raises(ConfirmationExpired):
        start_run(eval_db, run_id, code, now=NOW + timedelta(minutes=c.CONFIRMATION_MINUTES + 1))
    assert store.get_run(eval_db, run_id)["status"] == c.RUN_ESTIMATED
    start_run(eval_db, run_id, code, now=NOW + timedelta(minutes=c.CONFIRMATION_MINUTES - 1))


def test_a_confirmed_run_continues_its_later_batches_under_its_confirmation(rig, eval_db):
    run_id, _estimate, code = prepared(rig, eval_db, ["f04-stamp-duty"])
    start_run(eval_db, run_id, code, now=NOW)
    store.update_run(eval_db, run_id, status=c.RUN_PAUSED)
    start_run(eval_db, run_id, code, now=NOW + timedelta(days=2))  # the next day's batch


def test_the_estimate_names_the_shared_keys_and_the_window(rig, eval_db):
    _run_id, estimate, _code = prepared(rig, eval_db, ["f04-stamp-duty"])
    assert estimate["valid_minutes"] == c.CONFIRMATION_MINUTES
    assert set(estimate["shared_with_production"]) == {"jev", "embedding", "rerank"}
    text = "\n".join(est.describe(estimate))
    assert "production" in text and str(c.CONFIRMATION_MINUTES) in text


def test_the_estimate_uses_eighteen_judge_calls_per_case():
    assert est.JUDGE_TEXT_CALLS == 18


# ==== cost counted per call ====
def test_calls_are_charged_and_stored_as_they_happen_so_a_crash_loses_none(
    rig, eval_db, monkeypatch
):
    run_id, _estimate, code = prepared(rig, eval_db, ["m01-stt"])  # two turns
    start_run(eval_db, run_id, code, now=NOW)
    real = rig.pipeline.answer
    turns = []

    async def crash_on_second(request, db, **kwargs):
        turns.append(1)
        if len(turns) == 2:
            raise RuntimeError("the second turn broke")
        return await real(request, db, **kwargs)

    monkeypatch.setattr(rig.pipeline, "answer", crash_on_second)
    ex, run_ = executor(rig, eval_db, run_id)
    with pytest.raises(RuntimeError):
        asyncio.run(ex.run_batch(current_batch(run_)))
    used = store.usage_for_day(eval_db, NOW.strftime("%Y-%m-%d"))
    assert used["answer"][0] >= 1, "the first turn's calls were stored although the case failed"
    stored = store.get_run(eval_db, run_id)
    assert stored["requests_used"] == sum(r for r, _t in used.values())


def test_the_guard_stops_between_turns_not_in_the_middle_of_a_call():
    guard = CostGuard(1, 10)
    seen = []
    guard.on_charge = lambda who, r, t: seen.append((who, r, t))
    guard.charge(requests=1, tokens=5, who="answer", defer=True)
    guard.charge(requests=5, tokens=50, who="answer", defer=True)  # over, but deferred
    assert seen == [("answer", 1, 5), ("answer", 5, 50)]
    with pytest.raises(OverEstimate):
        guard.check()


def test_a_run_over_its_estimate_has_its_usage_rows_equal_to_what_it_counted(rig, eval_db):
    ids = ["f04-stamp-duty", "f01-penal-charge", "t01-spread-cr4"]
    run_id, _estimate, code = prepared(rig, eval_db, ids)
    start_run(eval_db, run_id, code, now=NOW)
    ex, run_ = executor(rig, eval_db, run_id, estimate_scale=0.05)
    asyncio.run(ex.run_batch(current_batch(run_)))
    used = store.usage_for_day(eval_db, NOW.strftime("%Y-%m-%d"))
    stored = store.get_run(eval_db, run_id)
    assert sum(r for r, _t in used.values()) == stored["requests_used"] > 0
    assert sum(t for _r, t in used.values()) == stored["tokens_used"] > 0


# ==== masked judge input ====
def test_the_question_a_judge_sees_is_the_masked_one(rig):
    case = rig.case("p01-pan")
    assert "ABCPE1234F" in case.question
    item = asyncio.run(rig.harness.run_case(case))
    assert "ABCPE1234F" not in item["question_masked"] and item["question_masked"]


def test_a_judged_run_sends_the_judge_no_identifier_and_stores_a_judged_row(rig, eval_db):
    store.sync_cases(eval_db, rig.cases)
    cases = [x for x in rig.cases if x.id in ("p01-pan", "f04-stamp-duty")]
    run_id, _estimate, code = prepare_run(
        eval_db, cases=cases, kind="active", environment="local", dataset_version=rig.version,
        config=config(), created_by="test", judged=True, now=NOW,
    )  # fmt: skip
    start_run(eval_db, run_id, code, now=NOW)
    gateway = RecordingGateway()
    run_ = store.get_run(eval_db, run_id)
    guard = guard_for(run_)
    ex = RunExecutor(
        eval_db, rig.harness, guard, Judge(gateway, guard=guard),
        {x.id: x for x in rig.cases}, run_id, now=lambda: NOW,
    )  # fmt: skip
    asyncio.run(ex.run_batch(current_batch(run_)))
    assert gateway.decides, "the judge was asked"
    assert "ABCPE1234F" not in json.dumps([d["state"] for d in gateway.decides])
    rows = [r for r in store.get_results(eval_db, run_id) if r["metric"] == "context_relevance"]
    assert rows and any(r["score"] is not None for r in rows)


# ==== scenarios re-embed the clause ====
def vector_of(rig, point_id):
    found = rig.qdrant.retrieve(COLLECTION, ids=[point_id], with_vectors=True)
    return list(found[0].vector["dense"])


def mf_points(rig):
    flt = models.Filter(
        must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=MF))]
    )
    found, _ = rig.qdrant.scroll(COLLECTION, scroll_filter=flt, limit=100, with_payload=True)
    return found


def test_an_edited_clause_is_re_embedded_and_put_back_exactly(rig):
    point = next(p for p in mf_points(rig) if "Stamp duty of 0.005" in p.payload["text"])
    before = vector_of(rig, point.id)

    async def embed(texts):
        return rig.embedder(list(texts), None)

    scenario = Scenario(rig.qdrant, COLLECTION, rig.tmp_path / "j.json", embed=embed)
    op = Op("edit_text", MF, anchor="Stamp duty of 0.005", old="0.005", new="0.010")
    apply(scenario, op)
    assert vector_of(rig, point.id) != before
    scenario.restore_all()
    assert vector_of(rig, point.id) == before


def test_without_an_embedder_an_edit_changes_the_lexical_vector_only(rig):
    point = next(p for p in mf_points(rig) if "Stamp duty of 0.005" in p.payload["text"])
    before = vector_of(rig, point.id)
    scenario = Scenario(rig.qdrant, COLLECTION, rig.tmp_path / "j.json")
    apply(scenario, Op("edit_text", MF, anchor="Stamp duty of 0.005", old="0.005", new="0.010"))
    assert vector_of(rig, point.id) == before
    scenario.restore_all()


def test_the_re_embedding_is_counted_as_an_embedding_call(rig, eval_db):
    run_id, _estimate, code = prepared(rig, eval_db, ["e01-edit-stamp-duty"])
    start_run(eval_db, run_id, code, now=NOW)
    ex, run_ = executor(rig, eval_db, run_id)
    asyncio.run(ex.run_batch(current_batch(run_)))
    assert rig.embed_calls, "the changed clause was embedded"
    used = store.usage_for_day(eval_db, NOW.strftime("%Y-%m-%d"))
    assert used["embedding"][0] >= 1


# ==== the hallucination rate and the reasons of a RAGAS miss ====
def _fake_ragas(monkeypatch, scores, reasons=None):
    import sys
    import types

    module = types.ModuleType("app.eval.ragas_adapter")
    module.METRICS = {"faithfulness": 1, "answer_relevancy": 1}

    class _Wrapper:
        def __init__(self, *_args, **_kwargs):
            pass

    async def score_sample(**kwargs):
        for name, why in (reasons or {}).items():
            kwargs["reasons"][name] = why
        return dict(scores)

    module.GatewayLLM = module.GatewayEmbeddings = _Wrapper
    module.score_sample = score_sample
    import app.eval

    monkeypatch.setitem(sys.modules, "app.eval.ragas_adapter", module)
    monkeypatch.setattr(app.eval, "ragas_adapter", module, raising=False)


def test_the_hallucination_rate_is_one_minus_faithfulness(monkeypatch):
    _fake_ragas(monkeypatch, {"faithfulness": 0.75, "answer_relevancy": 0.5})
    scores = {
        s.metric: s
        for s in run(
            judge_item(
                item_of("answer"), question="q", judge=Judge(RecordingGateway()), use_deepeval=False
            )  # fmt: skip
        )
    }
    assert scores["hallucination_rate"].value == pytest.approx(0.25)
    assert scores["faithfulness"].decider.startswith("ragas:")


def test_a_ragas_metric_that_could_not_be_computed_keeps_its_reason(monkeypatch):
    _fake_ragas(monkeypatch, {"faithfulness": None, "answer_relevancy": 0.5},
                {"faithfulness": "providers_failed"})  # fmt: skip
    scores = {
        s.metric: s
        for s in run(
            judge_item(
                item_of("answer"), question="q", judge=Judge(RecordingGateway()), use_deepeval=False
            )  # fmt: skip
        )
    }
    assert scores["faithfulness"].value is None
    assert scores["faithfulness"].decider == "not_scored:providers_failed"
    assert "hallucination_rate" not in scores


# ==== fits(): the token dimension counts ====
def test_a_need_that_fits_the_requests_but_not_the_tokens_does_not_fit():
    limits = est.DailyLimits(requests={"judge": 100}, tokens={"judge": 1_000}, keys={"judge": 1})
    need = est.Need()
    need.add("judge", 10, 2_000)
    assert est.fits(need, limits, 0.8) is False
    small = est.Need()
    small.add("judge", 10, 500)
    assert est.fits(small, limits, 0.8) is True


# ==== judge-samples and calibrate write usage rows and a run row ====
def test_calibrate_writes_a_run_and_usage_rows_for_every_call(tmp_path, monkeypatch):
    from app.eval import cli
    from app.eval.cases import load_calibration
    from tests.test_eval_judge import ScriptedGateway, labels_for

    monkeypatch.setattr(cli, "WORKDIR", tmp_path / "work")
    keys = {
        "EVAL_GROQ_API_KEYS": "g" * 14,
        "EVAL_GEMINI_API_KEYS": "m" * 14,
        "EVAL_OPENROUTER_API_KEYS": "o" * 14,
        "JEV_OPENROUTER_API_KEY": "v" * 14,
    }
    dbfile = str(tmp_path / "e.db")
    lines: list[str] = []
    cli.main(["migrate", "--db", dbfile], dict(keys), out=lines.append)
    lines.clear()
    cli.main(["calibrate", "--db", dbfile], dict(keys), out=lines.append)
    code = lines[-1].split("--confirm ")[1].strip()
    gateway = ScriptedGateway(labels_for(load_calibration()))
    gateway.config = type("C", (), {"eval_judge_model": JUDGE, "models": {}})()
    gateway.close = lambda: None
    services = type("S", (), {"gateway": gateway, "close": staticmethod(lambda: None)})()
    lines.clear()
    status = cli.main(
        ["calibrate", "--db", dbfile, "--confirm", code], dict(keys),
        services_factory=lambda env: services, out=lines.append,
    )  # fmt: skip
    assert status == 0, lines
    db = store.open_db(store.sqlite_url(tmp_path / "e.db"))
    try:
        (run_row,) = [
            r for r in store.list_runs(db, environment="local") if r["kind"] == "calibration"
        ]
        assert run_row["status"] == c.RUN_FINISHED and run_row["requests_used"] == 60
        used = store.usage_for_day(db, datetime.now(UTC).strftime("%Y-%m-%d"))
        assert sum(r for r, _t in used.values()) == 60
    finally:
        db.close()


# ==== the test collection name ====
@pytest.mark.parametrize("name", ["documents_eval", "test1_eval", "a_eval"])
def test_a_plain_lowercase_name_ending_in_eval_is_a_test_collection(name):
    assert c.check_eval_collection(name) == name


@pytest.mark.parametrize(
    "name",
    ["documents", "", "DOCUMENTS_eval", "my_docs_eval", "documents_eval2", "_eval", "x-y_eval",
     "documents_eval.bak", "../documents_eval"],
)  # fmt: skip
def test_any_other_name_is_not_a_test_collection(name):
    with pytest.raises(c.EvalConfigError):
        c.check_eval_collection(name)


def test_an_edit_case_is_priced_with_its_re_embedding(rig):
    plain = est.case_need(rig.case("f04-stamp-duty"))
    edited = est.case_need(rig.case("e01-edit-stamp-duty"))
    assert edited.requests["embedding"] >= plain.requests["embedding"] + 1
    assert edited.tokens["embedding"] > plain.tokens["embedding"]
