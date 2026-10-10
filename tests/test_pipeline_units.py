"""Small units of the pipeline: the owner's numbers, triage parsing and rules, the lock, the queue,
the
masker holder, the values cache, the fixed texts."""

import threading
from types import SimpleNamespace

import pytest

from app.gateway.decisions import ChoiceAnswer, NoulAnswer
from app.pipeline import messages
from app.pipeline.config import BUDGET_SECONDS, NODE_LIMITS, PipelineConfig
from app.pipeline.locks import SessionLocks
from app.pipeline.retrieval import ValuesCache, chunk_from_point, lexical_relevance
from app.pipeline.rewrite import needs_rewrite, parse_rewrite
from app.pipeline.startup import MaskerHolder
from app.pipeline.state import Triage
from app.pipeline.triage import build_questions, read_answers, rules_triage, valid_probability
from tests.pipeline_rig import masker_holder


# ---- the owner's numbers (all [T] for the Phase 16 baseline)
# --------------------------------------------
def test_the_starting_values_are_the_owners():
    config = PipelineConfig()
    assert BUDGET_SECONDS == config.budget_seconds == 30.0
    assert (config.candidates, config.top_k, config.rrf_k) == (25, 5, 60)
    assert config.relevance_floor == 0.30 and config.low_confidence_score == 0.40
    assert config.out_of_domain_probability == 0.60 and config.soft_filter_probability == 0.80
    assert config.lexical_floor > config.relevance_floor
    assert config.generator_retries == 1 and config.estimated_request_tokens == 3_400
    assert {n: v[0] for n, v in NODE_LIMITS.items() if n in ("triage", "rewrite", "embed")} == {
        "triage": 8.0,
        "rewrite": 8.0,
        "embed": 6.0,
    }
    assert NODE_LIMITS["retrieve"][0] == 4.0 and NODE_LIMITS["rerank"][0] == 6.0
    assert NODE_LIMITS["generate"][0] == 12.0 and NODE_LIMITS["cache_lookup"][0] == 1.0
    assert config.limits_for("a_local_stage") == (2.0, 0.0)


# ---- triage
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.0, 0.0), (1.0, 1.0), (0.5, 0.5), (1, 1.0), (float("nan"), None), (float("inf"), None),
     (-0.0001, None), (1.0001, None), (True, None), ("0.5", None), (None, None)],
)  # fmt: skip
def test_only_a_finite_probability_in_zero_to_one_is_used(value, expected):
    assert valid_probability(value) == expected


def test_the_rules_triage_never_says_out_of_domain_and_gives_no_filter_or_injection_score():
    triage = rules_triage("and for seniors?", first_message=False)
    assert triage.decider == "rules" and triage.needs_history == 1.0
    assert triage.in_domain_probability is None and triage.injection_score is None
    assert triage.product_line is None and triage.clause_type is None
    assert rules_triage("hello", first_message=True).vague == 1.0
    assert (
        rules_triage("hello", first_message=False).vague == 0.0
    )  # vague counts for a first message only
    assert (
        rules_triage("What is the waiting period for cataract surgery?", True).needs_history == 0.0
    )


def test_the_questions_asked_are_the_four_fixed_ones_plus_the_stored_values():
    ids = [q.id for q in build_questions({"product_line": [], "clause_type": []})]
    assert ids == ["domain_relevance", "needs_history", "vague_first_message", "injection_attempt"]
    ids = [q.id for q in build_questions({"product_line": ["a", "b"], "clause_type": ["x"]})]
    assert ids[-1] == "product_line" and "clause_type" not in ids  # a choice needs two options


def test_answers_are_read_with_their_probabilities_and_bad_ones_are_dropped():
    events = []
    answers = {
        "domain_relevance": NoulAnswer(0.9),
        "needs_history": NoulAnswer(float("nan")),
        "injection_attempt": NoulAnswer(0.1),
        "product_line": ChoiceAnswer("health", {"health": 0.85, "motor": 0.15}, 0.85),
        "clause_type": ChoiceAnswer("x", {"x": 2.0}, 0.9),  # an impossible probability
    }
    triage = read_answers(answers, lambda name, **fields: events.append((name, fields)))
    assert triage.in_domain_probability == 0.9 and triage.needs_history is None
    assert (triage.product_line, triage.product_line_probability) == ("health", 0.85)
    assert triage.clause_type is None and triage.vague is None
    assert [e[0] for e in events] == ["triage_probability_invalid"] * 2


# ---- rewrite decision
# -----------------------------------------------------------------------------------------
def rewrite_ctx(first, session=True, needs=0.0, vague=0.0):
    return SimpleNamespace(
        deps=SimpleNamespace(config=PipelineConfig()),
        triage=Triage(needs_history=needs, vague=vague),
        first_message=first,
        session_state=object() if session else None,
    )


@pytest.mark.parametrize(
    ("first", "session", "needs", "vague", "expected"),
    [
        (False, True, 0.9, 0.0, True),  # a follow-up that needs history
        (False, True, 0.4, 0.0, False),
        (True, False, 0.0, 0.9, True),  # a vague first message
        (True, False, 0.9, 0.0, True),  # a first message that "needs history" has none: clarify
        (True, False, 0.2, 0.2, False),
        (False, True, 0.0, 0.9, False),  # vague only matters for a first message
        (False, False, 0.9, 0.0, False),
    ],
)
def test_the_rewrite_runs_only_when_needed(first, session, needs, vague, expected):
    assert needs_rewrite(rewrite_ctx(first, session, needs, vague)) is expected


def test_a_rewrite_reply_must_be_an_object_with_a_boolean_clear():
    ok = '{"standalone_question": "q", "clear": true}'
    assert parse_rewrite(ok)["clear"] is True and parse_rewrite({"clear": False})["clear"] is False
    for bad in ("nope", "[1]", '{"clear": "yes"}', {"standalone_question": "q"}, None):
        assert parse_rewrite(bad) is None


# ---- retrieval helpers
# ----------------------------------------------------------------------------------------
def test_a_point_becomes_a_context_chunk_with_the_citation_fields():
    point = SimpleNamespace(
        id="p1",
        score=0.5,
        payload={
            "text": "Waiting period 30 days.",
            "doc_id": "d",
            "source": "a.pdf",
            "effective_date": "2026-10-01",
            "clause_ref": "2.1",
            "heading_path": ["Section 2", "Waiting"],
            "page_start": 3,
            "page_end": 4,
            "content_hash": "h",
        },
    )
    chunk = chunk_from_point(point)
    assert (chunk.chunk_id, chunk.clause_ref, chunk.heading_path, chunk.page_end) == (
        "p1",
        "2.1",
        ("Section 2", "Waiting"),
        4,
    )
    assert chunk_from_point(SimpleNamespace(id="p2", score=1.0, payload={"text": "  "})) is None
    assert chunk_from_point(SimpleNamespace(id="p3", score=1.0, payload=None)) is None


def test_lexical_relevance_is_the_share_of_the_questions_words_found_in_the_chunk():
    chunk = "A waiting period of 30 days applies to illness claims."
    assert lexical_relevance("waiting period illness claims", chunk) == 1.0
    assert lexical_relevance("waiting period zebra quantum", chunk) == 0.5
    assert lexical_relevance("", chunk) == 0.0


def test_the_values_cache_reloads_on_a_new_corpus_version_or_after_its_ttl_without_one():
    loads = []

    class Client:
        def facet(self, collection, key, facet_filter, limit):
            loads.append(key)
            return SimpleNamespace(hits=[SimpleNamespace(value=f"{key}-1")])

    cache, client = ValuesCache(ttl=300.0), Client()
    first = cache.get(client, "documents", 5, now=0.0)
    cache.get(client, "documents", 5, now=100.0)
    assert len(loads) == 2 and first["product_line"] == ["product_line-1"]  # one load of two fields
    cache.get(client, "documents", 6, now=101.0)  # the corpus changed
    assert len(loads) == 4
    cache.get(client, "documents", None, now=102.0)  # version unknown: reload...
    assert len(loads) == 6
    cache.get(client, "documents", None, now=103.0)  # ...but not on every call
    assert len(loads) == 6
    cache.get(client, "documents", None, now=1000.0)  # until the ttl passes
    assert len(loads) == 8


# ---- the lock, the holder
# ----------------------------------------------------------------------------------------
def test_a_session_is_held_once_and_a_release_frees_it():
    locks = SessionLocks()
    assert locks.try_acquire("u", "s") and not locks.try_acquire("u", "s")
    assert locks.try_acquire("u", "other") and locks.try_acquire(
        "v", "s"
    )  # keyed by user and session
    assert locks.held() == 3
    locks.release("u", "s")
    locks.release("u", "s")  # releasing twice is harmless
    assert locks.try_acquire("u", "s") and locks.held() == 3


def test_the_lock_is_safe_under_threads():
    locks, winners = SessionLocks(), []

    def grab():
        if locks.try_acquire("u", "s"):
            winners.append(1)

    threads = [threading.Thread(target=grab) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(winners) == 1


def test_the_holder_loads_in_the_background_warms_up_and_reports_failure():
    made = []
    holder = MaskerHolder(lambda: made.append(1) or masker_holder().masker)
    assert not holder.ready
    with pytest.raises(RuntimeError, match="not ready"):
        _ = holder.masker
    holder.start()
    holder.start()  # starting twice loads once
    assert holder.wait(10) and holder.ready and made == [1]
    assert MaskerHolder.ready_with(holder.masker).ready


# ---- the fixed texts
# ----------------------------------------------------------------------------------------------
def test_the_fixed_texts_are_plain_and_repeat_nothing_the_user_wrote():
    for name in dir(messages):
        value = getattr(messages, name)
        if name.isupper() and isinstance(value, str) and name != "CLARIFY":
            assert "{" not in value and "<" not in value, name
    assert messages.CLARIFY.format(understood="u. ", question="q?") == "u. q?"
