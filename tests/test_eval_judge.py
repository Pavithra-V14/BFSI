"""The judge: model family, Jev trust, "not scored", the cost guard, calibration (Phase 13).

Rules guarded here: the judge is never of the family of a model that wrote the answer (at
configuration and again per answer, also after fallbacks); Jev decides a metric only after
calibration trusted it; nobody to answer means None, never a number; every call is counted; the
judge's text calls and its verdicts use the evaluation key pool only."""

import json

import pytest

from app.eval import calibration as cal
from app.eval import store
from app.eval.guard import CostGuard, OverEstimate
from app.eval.judge import Judge, JudgeUnavailable
from app.eval.judging import judge_item
from app.gateway import questions as qs
from app.gateway.config import GatewayConfig, judge_model_problem, model_family
from app.gateway.errors import AllProvidersFailed, ProviderUnavailable
from app.gateway.keys import ConfigError as KeysConfigError
from tests.helpers import (
    FakeBackend,
    FakeDecisionClient,
    fake_key,
    make_config,
    make_gateway,
    ok,
    run,
)

JUDGE = "meta-llama/llama-4-maverick"
EVAL_KEY = fake_key("eval-openrouter")


def verdict_text(**by_id):
    """A chat model's JSON answer to typed yes/no questions."""
    return json.dumps({k: {"type": "noul", "noul": v} for k, v in by_id.items()})


def noul_response(value):
    def respond(call, n):
        answers = {
            qid: {"type": "noul", "noul": value(qid) if callable(value) else value}
            for qid in call["questions"]
        }
        return {
            "id": "gen-1",
            "model": "typesafe/jev-1.13",
            "provider": "TypeSafe",
            "answers": answers,
            "usage": {"input_tokens": 100, "output_tokens": 10, "cost": 0.00001},
        }

    return respond


def make_judge(*, jev=None, backend=None, judge_model=JUDGE, trusted=None, guard=None):
    client = FakeDecisionClient(jev or (lambda call, n: ProviderUnavailable()))
    chat = backend or FakeBackend(
        lambda model, key, n: ok(verdict_text(**{"chunk_relevant_0": 0.9}))
    )
    config = make_config(eval_judge_model=judge_model)
    gateway = make_gateway(config, decision_client=client, backend=chat)
    return Judge(gateway, guard=guard, trusted=trusted), client, chat


# ============================================================================ the family ====
@pytest.mark.parametrize(
    ("model", "family"),
    [
        ("openai/gpt-oss-120b", "openai"),
        ("gemini-3.8-flash", "google"),
        ("google/gemma-4-31b-it:free", "google"),
        ("gemma-3-27b-it", "google"),
        ("meta-llama/llama-4-maverick", "meta"),
        ("qwen/qwen3-235b", "alibaba"),
        ("mistralai/mistral-large", "mistral"),
        ("deepseek/deepseek-chat", "deepseek"),
        ("anthropic/claude-sonnet", "anthropic"),
        ("somevendor/some-model", "somevendor"),
    ],
)
def test_a_model_name_maps_to_its_vendor_family(model, family):
    assert model_family(model) == family


def test_gemini_and_gemma_are_one_family_so_the_default_fallback_clashes_with_gemini():
    assert model_family("gemini-3.8-flash") == model_family("google/gemma-4-31b-it:free")


@pytest.mark.parametrize(
    "judge", ["openai/gpt-4o-mini", "gemini-2.5-pro", "google/gemma-3-27b", "openai/gpt-oss-20b"]
)
def test_a_judge_of_the_family_of_an_answer_model_stops_the_gateway(judge):
    with pytest.raises(KeysConfigError) as caught:
        make_config(eval_judge_model=judge)
    assert "EVAL_JUDGE_MODEL" in str(caught.value) and judge not in str(caught.value)


def test_a_judge_of_another_family_is_accepted_and_none_means_not_scored():
    assert make_config(eval_judge_model=JUDGE).eval_judge_model == JUDGE
    assert make_config().eval_judge_model is None
    assert judge_model_problem(JUDGE, make_config().models) is None
    assert judge_model_problem(None, make_config().models) is None


def test_the_family_rule_follows_a_changed_answer_model():
    answers = {"GROQ_MODEL": "meta-llama/llama-3.3-70b", "GEMINI_MODEL": "gemini-3.8-flash",
               "OPENROUTER_MODEL": "google/gemma-4-31b-it:free"}  # fmt: skip
    assert judge_model_problem(JUDGE, answers) == "GROQ_MODEL"


def test_the_judge_model_is_read_from_the_environment_and_unset_by_default():
    base = {"GROQ_API_KEYS": "k" * 12}
    assert GatewayConfig.from_env(base).eval_judge_model is None
    with_judge = GatewayConfig.from_env({**base, "EVAL_JUDGE_MODEL": JUDGE})
    assert with_judge.eval_judge_model == JUDGE
    with pytest.raises(KeysConfigError):
        GatewayConfig.from_env({**base, "EVAL_JUDGE_MODEL": "openai/gpt-4o"})


# ======================================================================= the eval profile ====
def test_an_evaluation_gateway_answers_with_the_evaluation_keys_only():
    env = {
        "GROQ_API_KEYS": "production-groq-key",
        "GEMINI_API_KEYS": "production-gemini-key",
        "OPENROUTER_API_KEYS": "production-or-key",
        "EVAL_GROQ_API_KEYS": "evaluation-groq-key",
        "EVAL_GEMINI_API_KEYS": "evaluation-gemini-key",
        "EVAL_OPENROUTER_API_KEYS": "evaluation-or-key",
        "GATEWAY_ENABLE_MOCK": "true",
    }
    config = GatewayConfig.for_evaluation(env)
    assert config.groq_keys == ["evaluation-groq-key"]
    assert config.gemini_keys == ["evaluation-gemini-key"]
    assert config.openrouter_keys == [] and config.eval_openrouter_keys == ["evaluation-or-key"]
    assert config.enable_mock is False  # a mock answer must never be scored
    shown = repr(config)
    assert "production" not in shown and "evaluation-" not in shown  # no key in the repr


def test_an_evaluation_gateway_refuses_render_and_a_missing_key():
    with pytest.raises(KeysConfigError):
        GatewayConfig.for_evaluation({"RENDER": "true", "EVAL_GROQ_API_KEYS": "x" * 12})
    with pytest.raises(KeysConfigError):
        GatewayConfig.for_evaluation({"GROQ_API_KEYS": "production-only-key"})


# ======================================================================== gateway calls ====
def test_use_jev_false_never_asks_jev_and_uses_the_judge_model_with_the_eval_key():
    judge, client, chat = make_judge(jev=noul_response(0.9), trusted={})
    result = run(
        judge.verdicts("chunk_relevant", ["p0"], {"question": "q", "passages": {"p0": "t"}})
    )
    assert client.calls == [] and result.decider == "openrouter"
    assert chat.calls == [(f"openrouter/{JUDGE}", EVAL_KEY)]
    assert result.values == {"p0": True}


def test_a_trusted_metric_is_decided_by_jev_first():
    judge, client, chat = make_judge(jev=noul_response(0.2), trusted={"chunk_relevant": True})
    result = run(judge.verdicts("chunk_relevant", ["p0"], {"question": "q"}))
    assert result.decider == "jev" and result.values == {"p0": False}
    assert len(client.calls) == 1 and chat.calls == []


def test_trust_is_per_metric():
    judge, client, chat = make_judge(jev=noul_response(0.9), trusted={"claim_supported": True})
    run(judge.verdicts("chunk_relevant", ["p0"], {"question": "q"}))
    assert client.calls == []  # chunk_relevant was not calibrated
    run(judge.verdicts("claim_supported", ["p0"], {"claim": "c"}))
    assert len(client.calls) == 1


def test_a_trusted_metric_falls_to_the_judge_model_when_jev_is_down():
    judge, client, chat = make_judge(trusted={"chunk_relevant": True})
    result = run(judge.verdicts("chunk_relevant", ["p0"], {"question": "q"}))
    assert result.decider == "openrouter" and len(client.calls) == 1 and len(chat.calls) == 1


def test_nobody_to_answer_is_not_scored_never_a_number():
    judge, _client, chat = make_judge(judge_model=None, trusted={})
    result = run(judge.verdicts("chunk_relevant", ["p0", "p1"], {"question": "q"}))
    assert result.values == {"p0": None, "p1": None} and result.decider == "not_scored"
    assert chat.calls == []


def test_the_judge_never_judges_an_answer_written_by_a_model_of_its_own_family():
    judge, client, chat = make_judge(jev=noul_response(0.9), trusted={"chunk_relevant": True})
    # a fallback chain that ended on a model of the judge's family
    result = run(
        judge.verdicts(
            "chunk_relevant",
            ["p0"],
            {"question": "q"},
            models_used=["gemini-3.8-flash", "meta-llama/llama-3.1-70b"],
        )  # fmt: skip
    )
    assert result.values == {"p0": None} and result.reason == "judge_same_family"
    assert client.calls == [] and chat.calls == []  # not even Jev is asked: no call at all


def test_the_other_families_in_the_chain_do_not_block_the_judge():
    judge, _client, _chat = make_judge(trusted={})
    assert judge.family_clash(["openai/gpt-oss-120b", "gemini-3.8-flash"]) is False
    assert judge.family_clash([]) is False


def test_text_calls_use_the_judge_model_and_the_eval_pool_and_are_counted():
    guard = CostGuard(100, 100_000)
    chat = FakeBackend(lambda model, key, n: ok("a claim list", prompt=40, completion=10))
    judge, _client, chat = make_judge(backend=chat, guard=guard)
    assert run(judge.text("list the claims")) == "a claim list"
    assert chat.calls == [(f"openrouter/{JUDGE}", EVAL_KEY)]
    assert guard.requests == 1 and guard.tokens == 50 and guard.usage == {"judge": [1, 50]}


def test_a_text_call_without_a_judge_model_is_unavailable():
    judge, _client, chat = make_judge(judge_model=None)
    with pytest.raises(JudgeUnavailable):
        run(judge.text("anything"))
    assert chat.calls == []


def test_a_judge_that_is_down_raises_unavailable_not_a_made_up_text():
    chat = FakeBackend(lambda model, key, n: ProviderUnavailable())
    judge, _client, _chat = make_judge(backend=chat)
    with pytest.raises(JudgeUnavailable):
        run(judge.text("anything"))


def test_every_verdict_call_is_counted_by_group():
    guard = CostGuard(10, 10_000)
    judge, _c, _b = make_judge(
        jev=noul_response(0.9), trusted={"chunk_relevant": True}, guard=guard
    )
    run(judge.verdicts("chunk_relevant", ["p0"], {"question": "q"}))
    judge2, _c2, _b2 = make_judge(trusted={}, guard=guard)
    run(judge2.verdicts("chunk_relevant", ["p0"], {"question": "q"}))
    assert guard.usage["jev"][0] == 1 and guard.usage["judge"][0] == 1


def test_the_guard_stops_the_judge_when_the_run_is_over_its_estimate():
    guard = CostGuard(1, 100_000)
    judge, _c, _b = make_judge(guard=guard)
    run(judge.verdicts("chunk_relevant", ["p0"], {"question": "q"}))
    with pytest.raises(OverEstimate):
        run(judge.verdicts("chunk_relevant", ["p0"], {"question": "q"}))
        run(judge.verdicts("chunk_relevant", ["p0"], {"question": "q"}))


def test_complete_judge_with_no_model_raises_all_providers_failed():
    gateway = make_gateway(make_config())  # no judge model
    with pytest.raises(AllProvidersFailed):
        run(gateway.complete_judge([{"role": "user", "content": "hello there"}]))


def test_eval_verdict_questions_are_yes_no_questions():
    question = qs.eval_verdict("x", "Is it?", "Yes.", "No.")
    assert question.id == "x"


# ========================================================================== judge_item ====
class FakeGateway:
    """A gateway with just what the judge's helpers call."""

    def __init__(self, model="judge/model"):
        self.config = type("C", (), {"eval_judge_model": model})()
        self.decisions = []

    async def decide(self, questions, state, *, tier, max_wait_s, use_jev):
        from app.gateway.decisions import DecisionResult, NoulAnswer

        self.decisions.append((len(questions), use_jev))
        return DecisionResult(
            {q.id: NoulAnswer(0.9) for q in questions}, "jev" if use_jev else "openrouter",
            input_tokens=10, output_tokens=2,
        )  # fmt: skip

    async def complete_judge(self, messages, **kwargs):
        raise AllProvidersFailed([("judge", "down")])


ITEM = {
    "answer_kind": "answer",
    "answer_text": "Stamp duty of 0.005 per cent applies.",
    "contexts": ["Stamp duty of 0.005 per cent applies to purchases.", "Another passage."],
    "models": ["openai/gpt-oss-120b"],
    "cheap_checks": {"cited_idx": [0]},
}


def test_an_answered_item_gets_relevance_and_citation_verdicts():
    gateway = FakeGateway(model=None)  # no judge model; only chunk relevance is trusted for Jev
    judge = Judge(gateway, trusted={"chunk_relevant": True})
    scores = run(judge_item(ITEM, question="What stamp duty?", judge=judge))
    by_name = {s.metric: s for s in scores}
    assert (
        by_name["context_relevance"].value == 1.0 and by_name["context_relevance"].decider == "jev"
    )
    assert by_name["citation_accuracy"].decider == "openrouter"  # not trusted: no Jev for it
    assert gateway.decisions == [(2, True), (1, False)]
    assert (
        by_name["faithfulness"].value is None and by_name["faithfulness"].detail == "no_judge_model"
    )


def test_all_questions_about_one_item_go_in_one_call():
    gateway = FakeGateway()
    judge = Judge(gateway, trusted={})
    run(judge_item(ITEM, question="q", judge=judge, use_ragas=False, use_deepeval=False))
    assert [n for n, _ in gateway.decisions] == [2, 1]  # two passages, then one citation


def test_a_refusal_is_judged_for_correctness_only():
    gateway = FakeGateway()
    refusal = {
        "answer_kind": "refusal",
        "answer_text": "I cannot help.",
        "contexts": [],
        "models": [],
    }
    scores = run(judge_item(refusal, question="q", judge=Judge(gateway), use_ragas=False,
                            use_deepeval=False))  # fmt: skip
    assert [s.metric for s in scores] == ["refusal_correct"] and scores[0].value == 1.0


def test_an_item_of_the_judges_own_family_is_not_scored_at_all():
    gateway = FakeGateway(model=JUDGE)
    clash = {**ITEM, "models": ["meta-llama/llama-3.1-70b"]}
    scores = run(judge_item(clash, question="q", judge=Judge(gateway)))
    assert scores and all(s.value is None and s.detail == "judge_same_family" for s in scores)
    assert gateway.decisions == []


def test_a_greeting_has_nothing_to_judge():
    reply = {"answer_kind": "reply", "answer_text": "Hello.", "contexts": [], "models": []}
    assert run(judge_item(reply, question="hi", judge=Judge(FakeGateway()))) == []


# ============================================================================ calibration ====
def agreement_pairs(*pairs):
    return list(pairs)


def test_agreement_is_the_share_of_verdicts_equal_to_the_label_and_skips_not_scored():
    assert cal.agreement([(True, True), (False, False), (True, False), (None, True)]) == (
        pytest.approx(2 / 3),
        3,
    )
    assert cal.agreement([(None, True)]) == (None, 0)


def test_jev_is_trusted_only_at_the_threshold_and_with_enough_verdicts():
    assert cal.trusted(0.85, 6) is True and cal.trusted(0.84, 6) is False
    assert cal.trusted(1.0, 4) is False  # too few counted verdicts
    assert cal.trusted(None, 6) is False


class ScriptedGateway:
    """Jev answers every question with the label for 'good' metrics and the opposite for 'bad'."""

    def __init__(self, labels, bad=(), jev_down=()):
        self.labels, self.bad, self.jev_down = labels, set(bad), set(jev_down)
        self.config = type("C", (), {"eval_judge_model": JUDGE})()
        self.items = iter(())

    async def decide(self, questions, state, *, tier, max_wait_s, use_jev):
        from app.gateway.decisions import DecisionResult, NoulAnswer

        metric = state["verdict"]
        label = self.labels[(metric, state["order"][0], json.dumps(state["items"], sort_keys=True))]
        wrong = metric in self.bad
        decider = "jev" if use_jev and metric not in self.jev_down else "openrouter"
        answer = (not label) if (wrong and decider == "jev") else label
        return DecisionResult(
            {q.id: NoulAnswer(0.9 if answer else 0.1) for q in questions}, decider,
            input_tokens=5, output_tokens=1,
        )  # fmt: skip


def labels_for(items):
    out = {}
    for item in items:
        state = {"question": item.question, "passages": {"x": item.context}, "answer": item.answer}
        if item.metric in ("claim_supported", "citation_supports"):
            state = {"claim": item.answer, "passages": {"x": item.context}}
        elif item.metric == "injection_call_right":
            state = {"message": item.answer, "decision": item.context}
        out[(item.metric, "x", json.dumps(state, sort_keys=True))] = item.label
    return out


def test_calibration_trusts_jev_only_for_the_metrics_where_it_agrees():
    from app.eval.cases import load_calibration

    items = load_calibration()
    gateway = ScriptedGateway(labels_for(items), bad={"refusal_correct"})
    figures = run(cal.run_calibration(gateway, items))
    assert figures["claim_supported"]["trusted"] is True
    assert figures["refusal_correct"]["trusted"] is False
    assert figures["refusal_correct"]["jev_agreement"] == 0.0
    assert figures["claim_supported"]["judge_agreement"] == 1.0
    assert all(f["items"] == 6 and f["threshold"] == 0.85 for f in figures.values())


def test_a_jev_outage_does_not_count_as_jevs_verdict():
    from app.eval.cases import load_calibration

    items = load_calibration()
    gateway = ScriptedGateway(labels_for(items), jev_down={"chunk_relevant"})
    figures = run(cal.run_calibration(gateway, items))
    assert figures["chunk_relevant"]["jev_counted"] == 0
    assert figures["chunk_relevant"]["jev_agreement"] is None
    assert figures["chunk_relevant"]["trusted"] is False


def test_calibration_is_saved_and_read_back_as_the_trusted_set(tmp_path):
    from app.db.migrate import run as migrate

    url = store.sqlite_url(tmp_path / "e.db")
    migrate(url, store.MIGRATIONS, log=lambda line: None)
    db = store.open_db(url)
    figures = {
        "claim_supported": {"items": 6, "judge_agreement": 0.9, "jev_agreement": 0.95,
                            "threshold": 0.85, "trusted": True},
        "refusal_correct": {"items": 6, "judge_agreement": 0.9, "jev_agreement": 0.5,
                            "threshold": 0.85, "trusted": False},
    }  # fmt: skip
    cal.save(db, figures)
    assert cal.trusted_metrics(db) == {"claim_supported": True, "refusal_correct": False}
    db.close()


def test_the_calibration_estimate_counts_every_item_twice():
    from app.eval.cases import load_calibration

    items = load_calibration()
    estimate = cal.calibration_estimate(items, make_config())
    assert estimate["providers"]["judge"]["requests"] == 30
    assert estimate["providers"]["jev"]["requests"] == 30
    assert estimate["batches"] == [[i.id for i in items]]
