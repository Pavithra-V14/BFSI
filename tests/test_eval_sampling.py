"""The 5 % production sample (spec 15.4): masked text only, off the request path, never an error
to a user (Phase 13). Owner answer 2: Render samples, the judge runs on the admin machine."""

import json
from types import SimpleNamespace

import pytest

from app.db.connection import connect
from app.db.migrate import run as migrate
from app.eval import sampling, store
from app.eval.access import EvalAccess
from app.eval.sampling import Sampler
from tests.api_chat_fixtures import QUESTION, ChatEnv


@pytest.fixture
def eval_url(tmp_path):
    url = store.sqlite_url(tmp_path / "eval.db")
    migrate(url, store.MIGRATIONS, log=lambda line: None)
    return url


def fake_ctx(copy=("What is the waiting period?", "The waiting period is 30 days."), **extra):
    chunk = SimpleNamespace(chunk_id="c1", clause_ref="2", text="A waiting period of 30 days.")
    span = SimpleNamespace(model="openai/gpt-oss-120b")
    return SimpleNamespace(
        request=SimpleNamespace(role="customer"),
        stored_copy=copy,
        kept=[chunk],
        verdict=SimpleNamespace(
            cited_chunk_ids=("c1",),
            schema_ok=True,
            citation_ok=True,
            numeric_ok=True,
            grounded=True,
            coverage=0.9,
        ),
        trace=SimpleNamespace(spans=[span], audit={"prompt_hash": "ph", "corpus_version": 3}),
        **extra,
    )


def fake_result(kind="answer", **extra):
    values = {
        "kind": kind,
        "reason": None,
        "cached": False,
        "text": "The waiting period is 30 days.",
        "citations": [{"chunk_id": "c1"}],
        "warnings": [],
        "confidence": None,
        "degraded": [],
        "disclaimer": False,
        "tokens": 321,
        "timings_ms": {"a": 10.0, "b": 5.0},
    }
    values.update(extra)
    return SimpleNamespace(**values)


def make(eval_url, **kwargs):
    kwargs.setdefault("rate", 1.0)
    kwargs.setdefault("background", False)
    return Sampler(lambda: connect(eval_url), **kwargs)


def rows(eval_url):
    db = connect(eval_url)
    try:
        return db.query_all(
            "SELECT role, result_kind, question, answer, chunk_ids, contexts, models, "
            "prompt_hash, corpus_version, tokens, cheap_checks FROM eval_samples"
        )
    finally:
        db.close()


# ===================================================================== the decision ====
def test_a_live_answer_is_sampled_with_its_masked_copy_and_its_passages(eval_url):
    make(eval_url).observe(fake_ctx(), fake_result())
    (row,) = rows(eval_url)
    role, kind, question, answer, chunk_ids, contexts, models, prompt, corpus, tokens, cheap = row
    assert (role, kind) == ("customer", "answer")
    assert question == "What is the waiting period?" and answer == "The waiting period is 30 days."
    assert json.loads(chunk_ids) == ["c1"] and json.loads(contexts) == [
        "A waiting period of 30 days."
    ]
    assert json.loads(models) == ["openai/gpt-oss-120b"] and prompt == "ph" and corpus == "3"
    assert tokens == 321 and json.loads(cheap)["cited_idx"] == [0]


def test_the_rate_decides_and_a_draw_at_or_above_it_is_not_sampled(eval_url):
    sampler = make(eval_url, rate=0.05, rng=lambda: 0.049)
    sampler.observe(fake_ctx(), fake_result())
    sampler = make(eval_url, rate=0.05, rng=lambda: 0.05)
    sampler.observe(fake_ctx(), fake_result())
    assert len(rows(eval_url)) == 1


def test_the_default_rate_is_five_percent(eval_url):
    from app.eval import constants as c

    assert c.SAMPLE_RATE == 0.05 and Sampler(lambda: None).rate == 0.05  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["refusal", "reply", "clarify", "unavailable", "reword"])
def test_only_answers_are_sampled(eval_url, kind):
    make(eval_url).observe(fake_ctx(), fake_result(kind=kind))
    assert rows(eval_url) == []


def test_a_cached_answer_is_not_sampled(eval_url):
    make(eval_url).observe(fake_ctx(), fake_result(cached=True))
    assert rows(eval_url) == []


def test_without_a_masked_copy_nothing_is_stored(eval_url):
    make(eval_url).observe(fake_ctx(copy=None), fake_result())
    assert rows(eval_url) == []


def test_a_rate_of_zero_samples_nothing(eval_url):
    make(eval_url, rate=0.0).observe(fake_ctx(), fake_result())
    assert rows(eval_url) == []


# ============================================================================ the rules ====
def test_a_sample_holds_no_user_id_session_id_or_raw_text(eval_url):
    ctx = fake_ctx()
    ctx.request.user_id = "11111111-2222-3333-4444-555555555555"
    ctx.request.query = "RAW QUESTION WITH PAN ABCPE1234F"
    ctx.raw_query = "RAW QUESTION WITH PAN ABCPE1234F"
    make(eval_url).observe(ctx, fake_result(session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))
    stored = json.dumps([list(map(str, r)) for r in rows(eval_url)])
    assert "ABCPE1234F" not in stored and "11111111" not in stored and "aaaaaaaa" not in stored
    db = connect(eval_url)
    columns = [r[1] for r in db.query_all("PRAGMA table_info(eval_samples)")]
    db.close()
    assert not {"user_id", "session_id", "username", "email"} & set(columns)


def test_a_broken_database_is_an_event_with_the_class_name_never_an_error(eval_url):
    events = []

    def broken():
        raise ConnectionError("down, with a secret in it")

    clock = {"t": 0.0}
    sampler = Sampler(
        broken,
        rate=1.0,
        background=False,
        on_event=lambda n, f: events.append((n, f)),
        clock=lambda: clock["t"],
    )
    sampler.observe(fake_ctx(), fake_result())  # must not raise
    sampler.observe(fake_ctx(), fake_result())
    assert events == [("eval_sample_failed", {"reason": "ConnectionError"})]  # once a minute
    clock["t"] = 61.0
    sampler.observe(fake_ctx(), fake_result())
    assert len(events) == 2
    assert "secret" not in json.dumps(events)


def test_a_full_queue_drops_new_samples_and_counts_them(eval_url):
    sampler = Sampler(lambda: connect(eval_url), rate=1.0, background=True)
    for _ in range(sampling.QUEUE_SIZE + 7):
        sampler.observe(fake_ctx(), fake_result())
    assert sampler.dropped == 7
    assert sampler.flush() == sampling.QUEUE_SIZE


def test_the_request_path_only_queues_and_a_background_thread_writes(eval_url):
    sampler = Sampler(lambda: connect(eval_url), rate=1.0, background=True)
    sampler.observe(fake_ctx(), fake_result())
    assert rows(eval_url) == []  # nothing written on the request's own path
    sampler.start()
    sampler.stop()  # stop flushes what is queued
    assert len(rows(eval_url)) == 1


def test_a_hook_that_fails_cannot_stop_the_others():
    called = []

    def bad(ctx, result):
        raise RuntimeError("no")

    chained = sampling.chain(bad, lambda c, r: called.append("second"), None)
    chained(object(), object())
    assert called == ["second"]


# ============================================================================== settings ====
def test_no_database_address_means_no_sampling():
    assert sampling.sampler_from_env({}) is None
    assert sampling.sampler_from_env({"EVAL_DATABASE_URL": "  "}) is None


def test_the_rate_setting_is_checked_and_names_only_the_variable():
    assert sampling.rate_from_env({}) == 0.05
    assert sampling.rate_from_env({"EVAL_SAMPLE_RATE": "0.2"}) == 0.2
    for bad in ("5", "-0.1", "nan", "often"):
        with pytest.raises(sampling.SamplingConfigError) as caught:
            sampling.rate_from_env({"EVAL_SAMPLE_RATE": bad})
        assert "EVAL_SAMPLE_RATE" in str(caught.value) and bad not in str(caught.value)


def test_a_sampler_is_built_when_the_address_is_set(eval_url):
    sampler = sampling.sampler_from_env({"EVAL_DATABASE_URL": eval_url, "EVAL_SAMPLE_RATE": "1"})
    assert sampler is not None and sampler.rate == 1.0


# ==================================================================== through the API ====
def test_a_chat_is_sampled_through_the_pipeline_with_the_masked_question(
    tmp_path, fast_hashing, eval_url
):
    sampler = make(eval_url)
    env = ChatEnv(tmp_path, evaluation=EvalAccess(sampler=sampler, production_url=eval_url))
    try:
        _user, token = env.user_token("asha_rao")
        question = "My PAN is ABCPE1234F. " + QUESTION
        assert env.chat(token, question).status_code == 200
    finally:
        env.close()
    (row,) = rows(eval_url)
    stored = json.dumps([list(map(str, r)) for r in rows(eval_url)])
    assert "ABCPE1234F" not in stored  # the same masking as the stored transcript
    assert "waiting period" in row[2].lower() and row[0] == "customer" and row[1] == "answer"
    assert json.loads(row[5]), "the passages that reached the generator are kept for the judge"


def test_a_failing_sampler_never_changes_what_the_user_gets(tmp_path, fast_hashing):
    def broken():
        raise OSError("evaluation database down")

    sampler = Sampler(broken, rate=1.0, background=False)
    env = ChatEnv(tmp_path, evaluation=EvalAccess(sampler=sampler, production_url="x"))
    try:
        _user, token = env.user_token("asha_rao")
        response = env.chat(token)
        assert response.status_code == 200 and response.json()["kind"] == "answer"
    finally:
        env.close()
