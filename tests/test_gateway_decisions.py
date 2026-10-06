"""Typed decisions: Jev through the Decisions API, then chat models answering the same questions."""

import json

import httpx
import pytest

from app.gateway import questions as qs
from app.gateway.config import DEFAULT_PLANS
from app.gateway.decisions import (
    ChoiceAnswer,
    ChoiceQuestion,
    NoulAnswer,
    NoulQuestion,
    OpenRouterDecisionsClient,
    ScoreAnswer,
    ScoreQuestion,
    parse_fallback_answers,
    questions_payload,
    validate_questions,
)
from app.gateway.errors import (
    AuthFailed,
    BadRequest,
    CallTimeout,
    CreditExhausted,
    ProviderUnavailable,
    RateLimited,
    SchemaSurprise,
)
from tests.helpers import (
    FakeBackend,
    FakeDecisionClient,
    fake_key,
    lit,
    make_config,
    make_gateway,
    ok,
    run,
)

URGENCY = ScoreQuestion("urgency", "How urgent is it?", ["low", "medium", "high"])
QUESTIONS = [qs.domain_relevance(), qs.product_line(["banking", "insurance"]), URGENCY]
STATE = {"query": "what is the waiting period for pre-existing conditions"}

ANSWERS = {
    "domain_relevance": {"type": "noul", "noul": 0.96},
    "product_line": {
        "type": "choice",
        "choice": "insurance",
        "confidence": 0.67,
        "probabilities": {"banking": 0.22, "insurance": 0.78},
    },
    "urgency": {
        "type": "score",
        "score": 1.5,
        "confidence": 0.9,
        "probabilities": {"0": 0.0, "1": 0.5, "2": 0.5},
    },
}


def jev_response(answers=None):
    return {
        "id": "gen-dec-1",
        "model": "typesafe/jev-1.13-20260917",
        "provider": "TypeSafe",
        "answers": ANSWERS if answers is None else answers,
        "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.000019992},
    }


def chat_answer(model, key, n):
    return ok(json.dumps(ANSWERS), prompt=200, completion=80)


# ---- question builders -----------------------------------------------------------------


def test_question_payloads_follow_the_decisions_api_shape():
    payload = questions_payload(QUESTIONS)
    assert payload["domain_relevance"]["type"] == "noul"
    assert set(payload["domain_relevance"]["criteria"]) == {"true", "false"}
    assert payload["product_line"] == {
        "type": "choice",
        "instructions": "Which product line is the question about?",
        "criteria": {"banking": "banking", "insurance": "insurance"},
    }
    assert payload["urgency"]["criteria"] == ["low", "medium", "high"]


def test_builders_cover_the_required_questions():
    ids = {
        q.id
        for q in (
            qs.domain_relevance(),
            qs.needs_history(),
            qs.vague_first_message(),
            qs.injection_attempt(),
            qs.product_line(["a", "b"]),
            qs.clause_type({"x": "an x", "y": "a y"}),
            qs.clause_label(),
            qs.entity_candidate(),
            qs.entity_kind(),
            qs.eval_verdict("supported", "Is the claim supported?", "Yes.", "No."),
        )
    }
    assert ids == {
        "domain_relevance",
        "needs_history",
        "vague_first_message",
        "injection_attempt",
        "product_line",
        "clause_type",
        "clause_label",
        "entity_candidate",
        "entity_kind",
        "supported",
    }
    assert isinstance(qs.product_line(["a", "b"]), ChoiceQuestion)
    assert isinstance(qs.injection_attempt(), NoulQuestion)


def test_clause_label_has_the_eight_spec_categories():
    assert set(qs.clause_label().options) == {
        "exclusion",
        "waiting period",
        "KYC",
        "claims",
        "portability",
        "copayment",
        "grievance",
        "general",
    }


def test_question_validation():
    validate_questions(QUESTIONS)
    with pytest.raises(ValueError):
        validate_questions([])
    with pytest.raises(ValueError):
        validate_questions([qs.domain_relevance(), qs.domain_relevance()])
    with pytest.raises(ValueError):
        validate_questions([NoulQuestion("bad id!", "?", "y", "n")])
    with pytest.raises(ValueError):
        validate_questions([ChoiceQuestion("c", "?", {"only": "one"})])


# ---- Jev: the happy path and the request ------------------------------------------------


def test_jev_decides_with_typed_answers_and_records_the_snapshot_and_cost():
    client = FakeDecisionClient(lambda call, n: jev_response())
    gateway = make_gateway(decision_client=client)
    result = run(gateway.decide(QUESTIONS, STATE))
    assert result.decider == "jev" and result.calibrated is True
    assert result.model == "typesafe/jev-1.13"
    assert result.model_snapshot == "typesafe/jev-1.13-20260917"  # recorded: drift shows up
    assert result.provider == "TypeSafe" and result.fallback_reason is None
    assert result.answers["domain_relevance"] == NoulAnswer(0.96)
    assert result.answers["product_line"] == ChoiceAnswer(
        "insurance", {"banking": 0.22, "insurance": 0.78}, 0.67
    )
    assert isinstance(result.answers["urgency"], ScoreAnswer)
    assert result.cost_billed == pytest.approx(0.000019992)
    assert result.cost_source == "provider_reported" and result.shadow_cost > 0
    assert (result.input_tokens, result.output_tokens) == (476, 70)
    call = client.calls[0]
    assert call["model"] == "typesafe/jev-1.13" and call["state"] == STATE
    assert call["questions"] == questions_payload(QUESTIONS)
    assert call["key"] == fake_key("jev")


def test_jev_is_billed_by_default_and_free_plan_zeroes_the_billed_cost():
    client = FakeDecisionClient(lambda call, n: jev_response())
    result = run(make_gateway(decision_client=client).decide(QUESTIONS, STATE))
    assert result.cost_source == "provider_reported" and result.cost_billed > 0
    free = make_config(plans={**DEFAULT_PLANS, "JEV": "free"})
    result = run(make_gateway(free, decision_client=client).decide(QUESTIONS, STATE))
    assert (result.cost_billed, result.cost_source) == (0.0, "free_plan")
    assert result.shadow_cost > 0


def test_jev_is_never_the_router_or_the_latest_alias():
    client = FakeDecisionClient(lambda call, n: jev_response())
    run(make_gateway(decision_client=client).decide(QUESTIONS, STATE))
    assert "router" not in client.calls[0]["model"] and "latest" not in client.calls[0]["model"]


# ---- Jev unavailable: every surprise falls back ----------------------------------------


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (CreditExhausted(status=402), "credit_exhausted"),
        (AuthFailed(status=401), "auth_failed"),
        (CallTimeout(status=408), "timeout"),
        (ProviderUnavailable(status=503), "provider_unavailable"),
        (RateLimited(retry_after=5), "rate_limited"),
        (SchemaSurprise(), "schema_surprise"),
    ],
)
def test_any_jev_failure_falls_back_to_groq_20b_with_the_reason(error, reason):
    client = FakeDecisionClient(lambda call, n: error)
    backend = FakeBackend(chat_answer)
    gateway = make_gateway(decision_client=client, backend=backend)
    result = run(gateway.decide(QUESTIONS, STATE))
    assert result.decider == "groq" and result.calibrated is False
    assert result.model == "openai/gpt-oss-20b" and backend.calls[0][0] == "groq/openai/gpt-oss-20b"
    assert result.fallback_reason == f"jev: {reason}"
    assert result.answers["domain_relevance"] == NoulAnswer(0.96)
    assert any(name == "jev_unavailable" for name, _ in gateway.events)


@pytest.mark.parametrize(
    "bad_answers",
    [
        {},  # missing everything
        {**ANSWERS, "domain_relevance": {"type": "noul", "noul": "high"}},  # wrong value type
        {**ANSWERS, "domain_relevance": {"type": "noul", "noul": 1.7}},  # out of range
        {**ANSWERS, "domain_relevance": {"type": "choice", "noul": 0.9}},  # wrong type tag
        {**ANSWERS, "product_line": {**ANSWERS["product_line"], "choice": "marine"}},
        {**ANSWERS, "urgency": {**ANSWERS["urgency"], "score": 7.0}},
    ],
)
def test_a_schema_surprise_means_jev_unavailable_and_trips_the_breaker(bad_answers):
    client = FakeDecisionClient(lambda call, n: jev_response(bad_answers))
    gateway = make_gateway(decision_client=client, backend=FakeBackend(chat_answer))
    result = run(gateway.decide(QUESTIONS, STATE))
    assert result.decider == "groq" and result.fallback_reason == "jev: schema_surprise"
    run(gateway.decide(QUESTIONS, STATE))
    assert len(client.calls) == 1  # the open breaker stops calls to the alpha endpoint
    assert gateway.health()["jev"]["state"] == "circuit_open"


def test_a_non_dict_response_is_a_schema_surprise():
    client = FakeDecisionClient(lambda call, n: {"answers": "nope"})
    result = run(
        make_gateway(decision_client=client, backend=FakeBackend(chat_answer)).decide(
            QUESTIONS, STATE
        )
    )
    assert result.decider == "groq" and result.fallback_reason == "jev: schema_surprise"


def test_no_jev_key_or_too_large_a_state_skips_jev_without_a_call():
    client = FakeDecisionClient(lambda call, n: jev_response())
    no_key = make_gateway(
        make_config(jev_key=None), decision_client=client, backend=FakeBackend(chat_answer)
    )
    assert run(no_key.decide(QUESTIONS, STATE)).fallback_reason == "jev: no_keys"
    big = make_gateway(decision_client=client, backend=FakeBackend(chat_answer))
    result = run(big.decide(QUESTIONS, {"text": "word " * 33_000}))
    assert result.fallback_reason.startswith("jev: too_large") and client.calls == []
    assert "groq: too_large_for_key" in result.fallback_reason  # 8K tokens a minute per key


def test_the_fallback_chain_goes_on_to_gemini_then_openrouter_then_rules():
    client = FakeDecisionClient(lambda call, n: ProviderUnavailable())

    def handler(model, key, n):
        if model.startswith("groq/"):
            return lit("ServiceUnavailableError")
        if model.startswith("gemini/"):
            return ok("not json")  # invalid: skipped
        return ok(json.dumps(ANSWERS))

    gateway = make_gateway(decision_client=client, backend=FakeBackend(handler))
    result = run(gateway.decide(QUESTIONS, STATE))
    assert result.decider == "openrouter" and result.model == "google/gemma-4-31b-it:free"
    assert "gemini: invalid_output" in result.fallback_reason
    assert result.calibrated is False


def test_when_nothing_answers_the_result_says_use_rules():
    client = FakeDecisionClient(lambda call, n: ProviderUnavailable())
    failing = FakeBackend(lambda model, key, n: lit("ServiceUnavailableError"))
    gateway = make_gateway(decision_client=client, backend=failing)
    result = run(gateway.decide(QUESTIONS, STATE))
    assert result.answers is None and result.needs_rules and result.decider == "rules"
    assert result.fallback_reason.startswith("jev:")
    assert gateway.stats.shares("decide") == {"rules": 1.0}


def test_decide_chain_ends_with_the_answer_models():
    gateway = make_gateway(
        decision_client=FakeDecisionClient(lambda call, n: ProviderUnavailable())
    )
    models = [f"{s.provider}/{s.model}" for s in gateway.decide_steps()]
    assert models == [
        "groq/openai/gpt-oss-20b",
        "gemini/gemini-3.8-flash",
        "openrouter/google/gemma-4-31b-it:free",
        "groq/openai/gpt-oss-120b",
    ]  # duplicates of the same model are dropped


# ---- evaluation verdicts ---------------------------------------------------------------


def test_eval_verdict_without_a_judge_model_is_not_scored():
    client = FakeDecisionClient(lambda call, n: ProviderUnavailable())
    gateway = make_gateway(decision_client=client, backend=FakeBackend(chat_answer))
    question = [qs.eval_verdict("supported", "Is it supported?", "Yes.", "No.")]
    result = run(gateway.decide(question, STATE, tier="eval"))
    assert result.answers is None and result.decider == "not_scored"
    assert gateway._backend.calls == []  # no judge is configured, so no chat model was asked


def test_eval_verdict_uses_the_judge_model_and_eval_keys_when_jev_is_down():
    client = FakeDecisionClient(lambda call, n: ProviderUnavailable())
    answer = {"supported": {"type": "noul", "noul": 0.2}}
    backend = FakeBackend(lambda model, key, n: ok(json.dumps(answer)))
    config = make_config(eval_judge_model="some/judge-model")
    gateway = make_gateway(config, decision_client=client, backend=backend)
    question = [qs.eval_verdict("supported", "Is it supported?", "Yes.", "No.")]
    result = run(gateway.decide(question, STATE, tier="eval"))
    assert result.decider == "openrouter" and result.calibrated is False
    assert backend.calls == [("openrouter/some/judge-model", fake_key("eval-openrouter"))]


# ---- fallback JSON parsing -------------------------------------------------------------


def test_fallback_json_in_code_fences_and_missing_confidence_is_accepted():
    text = "```json\n" + json.dumps(ANSWERS) + "\n```"
    parsed = parse_fallback_answers(text, QUESTIONS)
    assert parsed["domain_relevance"] == NoulAnswer(0.96)
    shorter = {
        "domain_relevance": {"noul": 0.5},
        "product_line": {"choice": "banking", "probabilities": {"banking": 0.9, "insurance": 0.1}},
        "urgency": {"score": 0.0, "probabilities": {"0": 1.0, "1": 0.0, "2": 0.0}},
    }
    parsed = parse_fallback_answers(json.dumps(shorter), QUESTIONS)
    assert parsed["product_line"].confidence == 0.9 and parsed["urgency"].confidence == 1.0


def test_fallback_json_with_an_unknown_option_is_invalid():
    from app.gateway.errors import InvalidOutput

    bad = {**ANSWERS, "product_line": {**ANSWERS["product_line"], "choice": "marine"}}
    with pytest.raises(InvalidOutput):
        parse_fallback_answers(json.dumps(bad), QUESTIONS)


# ---- the Decisions API client ----------------------------------------------------------


def make_client(handler):
    return OpenRouterDecisionsClient(transport=httpx.MockTransport(handler))


def call_client(client):
    return run(
        client.decide(
            model="typesafe/jev-1.13",
            state=STATE,
            questions=questions_payload(QUESTIONS),
            api_key=fake_key("jev"),
            timeout=5,
        )
    )


def test_client_posts_to_the_alpha_decisions_endpoint_with_the_bearer_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=jev_response())

    data = call_client(make_client(handler))
    assert seen["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert seen["auth"] == f"Bearer {fake_key('jev')}"
    assert set(seen["body"]) == {"model", "state", "questions"}
    assert seen["body"]["model"] == "typesafe/jev-1.13"
    assert data["model"] == "typesafe/jev-1.13-20260917"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthFailed),
        (402, CreditExhausted),
        (400, BadRequest),
        (413, BadRequest),
        (429, RateLimited),
        (500, ProviderUnavailable),
        (502, ProviderUnavailable),
        (503, ProviderUnavailable),
        (524, ProviderUnavailable),
        (529, ProviderUnavailable),
    ],
)
def test_client_maps_error_statuses(status, expected):
    client = make_client(lambda request: httpx.Response(status, headers={"retry-after": "7"}))
    with pytest.raises(expected) as caught:
        call_client(client)
    assert caught.value.status == status or status == 429
    if status == 429:
        assert caught.value.retry_after == 7.0


def test_client_treats_timeouts_connection_errors_and_bad_json_as_unavailable():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(CallTimeout):
        call_client(make_client(timeout))
    with pytest.raises(ProviderUnavailable):
        call_client(make_client(refused))
    with pytest.raises(SchemaSurprise):
        call_client(make_client(lambda request: httpx.Response(200, content=b"<html>")))
    with pytest.raises(SchemaSurprise):
        call_client(make_client(lambda request: httpx.Response(200, json=["not", "a", "dict"])))


def test_client_errors_never_contain_the_response_body_or_the_key():
    body = f"leaked {fake_key('jev')}"
    client = make_client(lambda request: httpx.Response(500, text=body))
    with pytest.raises(ProviderUnavailable) as caught:
        call_client(client)
    assert fake_key("jev") not in str(caught.value) and "leaked" not in str(caught.value)
