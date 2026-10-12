"""Recorded API tests (spec 16): the real client code replays what the real providers answered.

The cassettes are made by the owner with `scripts/record_cassettes.py --run` (docs/OWNER_RUNS.md,
Run 25), never written by hand. Until a cassette is recorded its test is skipped with a plain
message. The replay never reaches a network (`record_mode="none"`): a request that is not in the
cassette is an error.

The first group tests the replay machinery itself on a throw-away cassette built in a temporary
folder from a typical response shape. It is NOT a recording and is never committed; it proves the
matching ignores keys, that a request outside the cassette cannot go out, and that a cut vector
is restored to its length, so the first real recording is not the first time this code runs."""

import asyncio
import json

import httpx
import pytest
import vcr.errors

from app.gateway.decisions import read_jev_response
from app.gateway.embeddings import DIMENSIONS, vectors_ok
from scripts import cassette_tools as tools

pytestmark = pytest.mark.recorded

HOW = "run scripts/record_cassettes.py --run (docs/OWNER_RUNS.md, Run 25)"


def need(name: str) -> None:
    if not tools.cassette_path(name).is_file():
        pytest.skip(f"cassette '{name}' is not recorded yet: {HOW}")


def keys() -> dict[str, str]:
    return {alias: tools.REPLAY_KEY for alias in tools.KEY_SETTINGS}


def run(coro):
    return asyncio.run(coro)


# ============================================================================== the real ones
def test_cohere_rerank_response_is_read_by_the_real_client():
    need("cohere_rerank")
    with tools.use_cassette("cohere_rerank"):
        items = run(tools.cohere_rerank(keys()))
    assert items and all(isinstance(i.index, int) and 0 <= i.index < 3 for i in items)
    scores = [i.score for i in items]
    assert all(isinstance(s, float) for s in scores) and scores == sorted(scores, reverse=True)


def test_jina_rerank_response_is_read_by_the_real_client():
    need("jina_rerank")
    with tools.use_cassette("jina_rerank"):
        items = run(tools.jina_rerank(keys()))
    assert items and all(0 <= i.index < 3 for i in items)


def test_jev_decisions_response_passes_the_schema_the_gateway_requires():
    need("jev_decisions")
    with tools.use_cassette("jev_decisions"):
        data = run(tools.jev_decisions(keys()))
    read = read_jev_response(data, tools.jev_questions())
    assert set(read["answers"]) == {q.id for q in tools.jev_questions()}
    assert read["input_tokens"] >= 0 and read["snapshot"]


def test_gemini_embeddings_come_back_as_one_usable_vector_per_input():
    need("gemini_embedding")
    with tools.use_cassette("gemini_embedding"):
        raw = run(tools.gemini_embedding(keys()))
    assert len(raw.vectors) == len(tools.EMBED_INPUTS)
    assert vectors_ok(raw.vectors, len(tools.EMBED_INPUTS)), "length restored, finite, not zero"
    assert all(len(v) == DIMENSIONS for v in raw.vectors)


def test_openrouter_chat_response_is_read_by_the_real_backend():
    need("openrouter_chat")
    with tools.use_cassette("openrouter_chat"):
        raw = run(tools.openrouter_chat(keys()))
    assert isinstance(raw.text, str) and raw.text.strip()
    assert raw.finish_reason in ("stop", "length")
    assert raw.prompt_tokens is None or raw.prompt_tokens >= 0


# ============================================================================== the machinery
@pytest.fixture
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "CASSETTE_DIR", tmp_path)
    return tmp_path


def write_cassette(folder, name, uri, body):
    data = {
        "version": 1,
        "interactions": [
            {
                "request": {"method": "POST", "uri": uri, "body": None, "headers": {}},
                "response": {
                    "status": {"code": 200, "message": "OK"},
                    "headers": {"Content-Type": ["application/json"]},
                    "body": {"string": json.dumps(body)},
                },
            }
        ],
    }
    (folder / f"{name}.json").write_text(json.dumps(data), "utf-8")


def test_the_replay_matches_without_the_key_and_reads_the_response(scratch):
    write_cassette(
        scratch,
        "cohere_rerank",
        "https://api.cohere.com/v2/rerank",
        {"results": [{"index": 2, "relevance_score": 0.2}, {"index": 0, "relevance_score": 0.8}]},
    )
    with tools.use_cassette("cohere_rerank"):
        items = run(tools.cohere_rerank({"cohere": "any-key-at-all-12"}))
    assert [i.index for i in items] == [0, 2]


def test_a_request_that_is_not_in_the_cassette_is_an_error_not_a_call(scratch):
    write_cassette(
        scratch,
        "cohere_rerank",
        "https://api.cohere.com/v2/rerank",
        {"results": [{"index": 0, "relevance_score": 0.8}]},
    )
    with pytest.raises((vcr.errors.CannotOverwriteExistingCassetteException, httpx.HTTPError)):
        with tools.use_cassette("cohere_rerank"):
            run(tools.jina_rerank({"jina": "any-key-at-all-12"}))  # another host


def test_a_cut_vector_in_a_cassette_is_replayed_at_its_recorded_length(scratch):
    body = {"embeddings": [{"values": [0.5, 0.25, 0.125], "values_original_length": 768}]}
    write_cassette(scratch, "gemini_embedding", "https://embed.example.test/v1/embed", body)

    async def call():
        async with httpx.AsyncClient() as client:
            reply = await client.post("https://embed.example.test/v1/embed", json={})
            return reply.json()

    with tools.use_cassette("gemini_embedding"):
        replayed = run(call())
    values = replayed["embeddings"][0]["values"]
    assert len(values) == 768 and set(values[3:]) == {0.0}  # length, not the numbers
    assert "values_original_length" not in replayed["embeddings"][0]


def test_a_missing_cassette_skips_with_a_plain_message(scratch):
    with pytest.raises(pytest.skip.Exception) as caught:
        need("jev_decisions")
    assert "not recorded yet" in str(caught.value) and "Run 25" in str(caught.value)
