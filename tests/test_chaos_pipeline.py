"""Chaos tests of the whole pipeline (spec 16): schedules of failures, slow providers and
recovery, seen from the user's side.

The rule in every test: the user gets a clean answer or the one fixed "temporarily unavailable"
text; never a provider name, a key, an error text or an ungrounded guess; nothing from a failed
request is stored; the next request after the failure ends is a normal answer."""

import asyncio
import time

import pytest

from app.gateway.errors import AllProvidersFailed, EmbeddingUnavailable
from app.pipeline import messages
from app.pipeline.config import PipelineConfig
from tests.pipeline_rig import PipelineRig

pytestmark = pytest.mark.chaos

QUESTIONS = [
    "What is the waiting period for illness claims?",
    "What is the waiting period for illness claims under the policy?",
    "Tell me the waiting period for illness claims please",
    "How long is the waiting period for illness claims?",
]
FORBIDDEN_IN_REPLY = ("groq", "gemini", "openrouter", "jev", "cohere", "jina", "429", "key")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def rig(tmp_path):
    made = PipelineRig(tmp_path)
    yield made
    made.close()


def clean_unavailable(result):
    assert result.kind == "unavailable" and result.text == messages.UNAVAILABLE
    shown = (result.text or "").lower()
    assert not any(word in shown for word in FORBIDDEN_IN_REPLY)
    assert not result.citations


def stored_messages(rig):
    return rig.db.query_one("SELECT COUNT(*) FROM messages")[0]


FAILURES = {
    "complete_error": AllProvidersFailed([("groq", "rate_limited"), ("gemini", "timeout")]),
    "embed_error": EmbeddingUnavailable([("gemini", "rate_limited")]),
}


@pytest.mark.parametrize("attribute", sorted(FAILURES))
def test_a_run_of_failures_is_always_the_fixed_text_and_the_next_request_recovers(rig, attribute):
    setattr(rig.gateway, attribute, FAILURES[attribute])
    for question in QUESTIONS[:3]:  # three failures in a row, none of them stored or cached
        clean_unavailable(run(rig.ask(question)))
    assert stored_messages(rig) == 0
    setattr(rig.gateway, attribute, None)  # the provider is back
    healthy = run(rig.ask(QUESTIONS[3]))
    assert healthy.kind == "answer" and healthy.text != messages.UNAVAILABLE
    assert stored_messages(rig) >= 1


def test_triage_down_for_a_while_never_shows_an_error_and_never_stops_the_answers(rig):
    """Jev and the decide models all fail: triage falls back to its rules, the answer tier is
    fine, so the user keeps getting answers (or a normal refusal), never an error text."""
    rig.gateway.decide_error = AllProvidersFailed([("jev", "timeout"), ("groq", "rate_limited")])
    for question in QUESTIONS[:3]:
        result = run(rig.ask(question))
        assert result.kind in ("answer", "refusal", "clarify")
        assert result.text != messages.UNAVAILABLE
        shown = (result.text or "").lower()
        assert not any(word in shown for word in FORBIDDEN_IN_REPLY)
    rig.gateway.decide_error = None
    assert run(rig.ask(QUESTIONS[3])).kind == "answer"


def test_failures_that_come_and_go_never_leave_a_wrong_answer_in_the_cache(rig):
    rig.gateway.complete_error = FAILURES["complete_error"]
    clean_unavailable(run(rig.ask(QUESTIONS[0])))
    rig.gateway.complete_error = None
    good = run(rig.ask(QUESTIONS[0]))
    assert good.kind == "answer"
    again = run(rig.ask(QUESTIONS[0]))  # the same words as the failed one
    assert again.kind == "answer" and again.text == good.text


def test_a_slow_provider_that_answers_inside_its_limit_is_a_normal_answer(rig):
    rig.gateway.delay["answer"] = 0.4
    result = run(rig.ask(QUESTIONS[0]))
    assert result.kind == "answer"
    assert result.timings_ms and sum(result.timings_ms.values()) >= 400


def test_a_provider_slower_than_the_budget_is_cut_and_the_user_is_not_kept_waiting(rig):
    rig.gateway.delay["answer"] = 30.0  # far longer than the budget below
    rig.deps.config = PipelineConfig(budget_seconds=1.5)
    started = time.monotonic()
    result = run(rig.ask(QUESTIONS[0]))
    elapsed = time.monotonic() - started
    clean_unavailable(result)
    assert elapsed < rig.gateway.delay["answer"] / 3, "cut long before the provider finished"
    assert stored_messages(rig) == 0
    rig.gateway.delay["answer"] = 0.0
    rig.deps.config = PipelineConfig()
    assert run(rig.ask(QUESTIONS[1])).kind == "answer"


def test_a_rate_limited_answer_tier_is_unavailable_and_exposes_nothing_of_the_provider(rig):
    rig.gateway.complete_error = AllProvidersFailed(
        [("groq", "rate_limited"), ("gemini", "rate_limited"), ("openrouter", "rate_limited")]
    )
    result = run(rig.ask(QUESTIONS[0]))
    clean_unavailable(result)
    blob = repr(result.audit) + repr(result.warnings) + repr(result.degraded)
    assert "retry" not in blob.lower() and "groq" not in blob.lower()
