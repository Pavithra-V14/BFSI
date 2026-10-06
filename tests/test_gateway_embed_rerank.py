"""Embeddings (key A then key B) and the rerank chain (Cohere, Jina, lexical)."""

import json
import math

import httpx
import pytest

from app.gateway.backend import RawEmbedding
from app.gateway.config import DEFAULT_PLANS
from app.gateway.embeddings import (
    DIMENSIONS,
    DevEmbedderForbidden,
    HashEmbedder,
    format_document,
    format_query,
    norm,
    normalise,
    running_on_render,
)
from app.gateway.errors import ContextWindowExceeded, EmbeddingUnavailable
from app.gateway.keys import fingerprint
from app.gateway.rerank import http_rerank, lexical_scores, top_items
from tests.helpers import FakeBackend, fake_key, lit, make_config, make_gateway, run

KEY_A, KEY_B = fake_key("embedA"), fake_key("embedB")


def unit_vector(size=DIMENSIONS):
    return [1.0] + [0.0] * (size - 1)


# ---- embeddings -------------------------------------------------------------------------


def test_prefixes_follow_the_documented_task_format():
    assert format_query("what is KYC") == "task: search result | query: what is KYC"
    assert format_document("a clause", "Section 3") == "title: Section 3 | text: a clause"
    assert format_document("a clause") == "title: none | text: a clause"


def test_embedding_uses_gemini_embedding_2_at_768_dimensions_with_key_a():
    backend = FakeBackend()
    gateway = make_gateway(backend=backend)
    result = run(gateway.embed(["first clause", "second clause"], task="document"))
    assert result.model == "gemini/gemini-embedding-2" and not result.is_dev_fallback
    assert len(result.vectors) == 2 and all(len(v) == DIMENSIONS for v in result.vectors)
    assert all(abs(norm(v) - 1.0) < 1e-9 for v in result.vectors)
    model, key, inputs = backend.embed_calls[0]
    assert (model, key) == ("gemini/gemini-embedding-2", KEY_A)
    assert inputs == ["title: none | text: first clause", "title: none | text: second clause"]
    assert result.key_fingerprint == fingerprint(KEY_A) and result.fallback_reason is None


def test_query_task_uses_the_query_prefix_and_titles_reach_documents():
    backend = FakeBackend()
    gateway = make_gateway(backend=backend)
    run(gateway.embed(["what is KYC"], task="query"))
    assert backend.embed_calls[0][2] == ["task: search result | query: what is KYC"]
    run(gateway.embed(["text"], task="document", titles=["Section 2"]))
    assert backend.embed_calls[1][2] == ["title: Section 2 | text: text"]


def test_vectors_that_are_not_unit_length_are_normalised():
    backend = FakeBackend()
    backend.embed_handler = lambda m, k, inputs, n: RawEmbedding(
        [[2.0] + [0.0] * (DIMENSIONS - 1) for _ in inputs], 3, 0.0, "litellm"
    )
    result = run(make_gateway(backend=backend).embed(["x"]))
    assert result.vectors[0][0] == pytest.approx(1.0)
    assert normalise([3.0, 4.0]) == pytest.approx([0.6, 0.8])


def test_key_b_is_used_when_key_a_fails():
    def handler(model, key, inputs, n):
        if key == KEY_A:
            return lit("ServiceUnavailableError")
        return RawEmbedding([unit_vector() for _ in inputs], 4, 0.0, "litellm")

    backend = FakeBackend()
    backend.embed_handler = handler
    result = run(make_gateway(backend=backend).embed(["x"]))
    assert result.key_fingerprint == fingerprint(KEY_B)
    assert result.fallback_reason == "primary key failed"
    assert [key for _, key, _ in backend.embed_calls] == [KEY_A, KEY_B]


def test_both_keys_failing_means_temporarily_unavailable_and_the_breaker_opens():
    backend = FakeBackend()
    backend.embed_handler = lambda model, key, inputs, n: lit("ServiceUnavailableError")
    gateway = make_gateway(backend=backend)
    with pytest.raises(EmbeddingUnavailable) as caught:
        run(gateway.embed(["x"]))
    assert caught.value.skipped[0][0] == "gemini_embedding"
    calls_before = len(backend.embed_calls)
    with pytest.raises(EmbeddingUnavailable) as again:
        run(gateway.embed(["x"]))
    assert again.value.skipped == [("gemini_embedding", "circuit_open")]
    assert len(backend.embed_calls) == calls_before
    assert any(name == "embedding_unavailable" for name, _ in gateway.events)


def test_a_wrong_dimension_from_the_provider_is_not_accepted():
    backend = FakeBackend()
    backend.embed_handler = lambda m, k, inputs, n: RawEmbedding(
        [[1.0, 0.0] for _ in inputs], 1, 0.0, "litellm"
    )
    with pytest.raises(EmbeddingUnavailable):
        run(make_gateway(backend=backend).embed(["x"]))


def test_inputs_are_sent_in_batches_and_an_oversized_text_is_refused():
    backend = FakeBackend()
    gateway = make_gateway(backend=backend)
    result = run(gateway.embed([f"clause {i}" for i in range(40)]))
    assert [len(c[2]) for c in backend.embed_calls] == [16, 16, 8] and len(result.vectors) == 40
    with pytest.raises(ContextWindowExceeded):
        run(gateway.embed(["word " * 9_000]))


def test_embedding_cost_has_a_shadow_value():
    result = run(make_gateway(backend=FakeBackend()).embed(["a clause of text"]))
    assert (result.cost_billed, result.cost_source) == (0.0, "free_plan")  # free tier
    assert result.shadow_cost > 0
    paid = make_config(plans={**DEFAULT_PLANS, "EMBEDDING": "paid"})
    result = run(make_gateway(paid, backend=FakeBackend()).embed(["a clause of text"]))
    assert result.cost_billed == pytest.approx(0.0001) and result.cost_source == "litellm"


def test_the_hash_embedder_is_development_only():
    assert not running_on_render({})
    assert running_on_render({"RENDER": "true"})
    vector = HashEmbedder({}).embed("hello")
    assert len(vector) == DIMENSIONS and abs(norm(vector) - 1.0) < 1e-9
    assert vector == HashEmbedder({}).embed("hello") != HashEmbedder({}).embed("world")
    with pytest.raises(DevEmbedderForbidden):
        HashEmbedder({"RENDER": "true"})


def test_the_hash_fallback_is_off_by_default_labelled_when_on_and_never_on_render():
    failing = FakeBackend()
    failing.embed_handler = lambda model, key, inputs, n: lit("ServiceUnavailableError")
    with pytest.raises(EmbeddingUnavailable):
        run(make_gateway(backend=failing).embed(["x"]))
    allowed = make_gateway(backend=failing, allow_dev_embedding=True, env={})
    result = run(allowed.embed(["x"]))
    assert result.is_dev_fallback and result.model == "hash-dev"
    assert result.fallback_reason == "provider_unavailable"
    no_eval_keys = make_config(eval_openrouter_keys=[])  # eval keys are refused on Render
    on_render = make_gateway(
        no_eval_keys, backend=failing, allow_dev_embedding=True, env={"RENDER": "true"}
    )
    with pytest.raises(EmbeddingUnavailable):
        run(on_render.embed(["x"]))


def test_no_embedding_keys_means_unavailable():
    gateway = make_gateway(make_config(embedding_keys=[]), backend=FakeBackend())
    with pytest.raises(EmbeddingUnavailable) as caught:
        run(gateway.embed(["x"]))
    assert caught.value.skipped == [("gemini_embedding", "no_keys")]


# ---- rerank -----------------------------------------------------------------------------

DOCS = [
    "Claims must be filed within thirty days.",
    "The waiting period for pre-existing diseases is thirty six months.",
    "Premiums are payable yearly.",
]


def cohere_json(order):
    return {"results": [{"index": i, "relevance_score": s} for i, s in order]}


def transport(handlers):
    """handlers: {'cohere': fn(request) -> Response, 'jina': ...}; unknown hosts raise."""

    def route(request: httpx.Request) -> httpx.Response:
        name = "cohere" if "cohere" in request.url.host else "jina"
        return handlers[name](request)

    return httpx.MockTransport(route)


def test_cohere_is_called_first_with_the_documented_request_and_sorted_scores():
    seen = {}

    def cohere(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=cohere_json([(2, 0.1), (1, 0.9), (0, 0.4)]))

    gateway = make_gateway(rerank_transport=transport({"cohere": cohere}))
    result = run(gateway.rerank("waiting period", DOCS, top_n=2))
    assert seen["url"] == "https://api.cohere.com/v2/rerank"
    assert seen["auth"] == f"Bearer {fake_key('cohere')}"
    assert seen["body"] == {
        "model": "rerank-v3.5",
        "query": "waiting period",
        "documents": DOCS,
        "top_n": 2,
    }
    assert result.reranker == "cohere" and not result.degraded
    assert [i.index for i in result.items] == [1, 0, 2]  # best first
    assert result.fallback_reason is None


@pytest.mark.parametrize("status", [500, 503, 429, 401])
def test_jina_is_used_when_cohere_fails(status):
    seen = {}

    def jina(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=cohere_json([(1, 0.8), (0, 0.2)]))

    handlers = {"cohere": lambda r: httpx.Response(status), "jina": jina}
    result = run(make_gateway(rerank_transport=transport(handlers)).rerank("waiting", DOCS))
    assert result.reranker == "jina" and not result.degraded
    assert seen["url"] == "https://api.jina.ai/v1/rerank"
    assert seen["body"]["model"] == "jina-reranker-v3"
    assert result.fallback_reason.startswith("cohere:")


def test_lexical_fallback_is_marked_degraded_when_both_rerankers_fail():
    handlers = {"cohere": lambda r: httpx.Response(500), "jina": lambda r: httpx.Response(503)}
    gateway = make_gateway(rerank_transport=transport(handlers))
    result = run(gateway.rerank("waiting period pre-existing diseases", DOCS, top_n=2))
    assert result.reranker == "lexical" and result.degraded
    assert [i.index for i in result.items][0] == 1 and len(result.items) == 2
    assert [p for p, _ in result.skipped] == ["cohere", "jina"]
    assert gateway.stats.shares("rerank") == {"lexical": 1.0}


def test_an_unexpected_rerank_shape_is_a_failure_not_a_crash():
    handlers = {
        "cohere": lambda r: httpx.Response(200, json={"oops": 1}),
        "jina": lambda r: httpx.Response(200, json=cohere_json([(9, 0.5)])),  # index out of range
    }
    result = run(make_gateway(rerank_transport=transport(handlers)).rerank("x", DOCS))
    assert result.reranker == "lexical" and result.degraded


def test_rerank_without_keys_goes_straight_to_lexical_and_empty_input_is_empty():
    gateway = make_gateway(make_config(cohere_key=None, jina_key=None))
    result = run(gateway.rerank("claims", DOCS))
    assert result.reranker == "lexical" and result.items[0].index == 0
    assert run(gateway.rerank("claims", [])).items == []


def test_http_rerank_timeout_and_connection_errors_are_typed():
    from app.gateway.errors import CallTimeout, ProviderUnavailable

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("no", request=request)

    def call(handler):
        return run(
            http_rerank(
                "cohere",
                model="m",
                query="q",
                documents=["d"],
                top_n=None,
                api_key=fake_key("cohere"),
                timeout=1,
                transport=httpx.MockTransport(handler),
            )
        )

    with pytest.raises(CallTimeout):
        call(timeout)
    with pytest.raises(ProviderUnavailable):
        call(refused)


def test_lexical_scores_rank_overlapping_text_first_and_handle_edge_cases():
    scores = lexical_scores("waiting period diseases", DOCS)
    assert scores.index(max(scores)) == 1 and all(math.isfinite(s) for s in scores)
    assert lexical_scores("", DOCS) == [0.0, 0.0, 0.0]
    assert lexical_scores("anything", []) == []
    assert [i.index for i in top_items([0.1, 0.9, 0.5], 2)] == [1, 2]
    assert [i.index for i in top_items([0.5, 0.5], None)] == [0, 1]  # ties keep input order
