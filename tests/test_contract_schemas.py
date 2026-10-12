"""Contract tests (spec 16): each structured output is checked against a table of what a provider
may return, good and bad. The code is offline; the tables are the contract.

What a bad payload must never do: reach a caller as a value, raise anything other than the
gateway's own typed error, or take the process down. A good payload must come out with the exact
types the rest of the code relies on."""

import asyncio
import json
import math

import httpx
import pytest

from app.gateway.decisions import (
    ChoiceAnswer,
    ChoiceQuestion,
    NoulAnswer,
    NoulQuestion,
    OpenRouterDecisionsClient,
    ScoreAnswer,
    ScoreQuestion,
    extract_json,
    parse_answers,
    parse_fallback_answers,
    read_jev_response,
)
from app.gateway.embeddings import DIMENSIONS, vectors_ok
from app.gateway.errors import GatewayError, InvalidOutput, SchemaSurprise
from app.gateway.rerank import http_rerank
from app.guardrails.output import MAX_ANSWER_CHARACTERS, MAX_CITATIONS, parse_answer

pytestmark = pytest.mark.contract

NOUL = NoulQuestion("domain_relevance", "Is it in scope?", "yes", "no")
CHOICE = ChoiceQuestion("product_line", "Which line?", {"banking": "banking", "insurance": "ins"})
SCORE = ScoreQuestion("urgency", "How urgent?", ["low", "medium", "high"])
QUESTIONS = [NOUL, CHOICE, SCORE]

GOOD = {
    "domain_relevance": {"type": "noul", "noul": 0.9},
    "product_line": {
        "type": "choice",
        "choice": "banking",
        "probabilities": {"banking": 0.8, "insurance": 0.2},
        "confidence": 0.8,
    },
    "urgency": {
        "type": "score",
        "score": 1.4,
        "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3},
        "confidence": 0.6,
    },
}


def replaced(**changes):
    return {**GOOD, **changes}


# ================================================================ typed decisions (Jev's shape)
def test_a_good_decision_payload_has_the_exact_types_the_pipeline_uses():
    parsed = parse_answers(GOOD, QUESTIONS)
    assert parsed["domain_relevance"] == NoulAnswer(0.9)
    assert isinstance(parsed["product_line"], ChoiceAnswer)
    assert parsed["product_line"].option == "banking"
    assert isinstance(parsed["urgency"], ScoreAnswer)


BAD_DECISIONS = {
    "not an object": ["a", "list"],
    "no answers": {},
    "one question missing": {k: v for k, v in GOOD.items() if k != "urgency"},
    "answer is a string": replaced(domain_relevance="0.9"),
    "wrong type tag": replaced(domain_relevance={"type": "choice", "noul": 0.9}),
    "noul as text": replaced(domain_relevance={"type": "noul", "noul": "high"}),
    "noul above one": replaced(domain_relevance={"type": "noul", "noul": 1.0001}),
    "noul below zero": replaced(domain_relevance={"type": "noul", "noul": -0.1}),
    "noul not a number": replaced(domain_relevance={"type": "noul", "noul": float("nan")}),
    "noul missing": replaced(domain_relevance={"type": "noul"}),
    "choice not offered": replaced(product_line={**GOOD["product_line"], "choice": "marine"}),
    "choice is a list": replaced(product_line={**GOOD["product_line"], "choice": ["banking"]}),
    "probability of an unknown option": replaced(
        product_line={**GOOD["product_line"], "probabilities": {"marine": 1.0}}
    ),
    "confidence above one": replaced(product_line={**GOOD["product_line"], "confidence": 7}),
    "score above the scale": replaced(urgency={**GOOD["urgency"], "score": 7.0}),
    "score as text": replaced(urgency={**GOOD["urgency"], "score": "high"}),
    "score below zero": replaced(urgency={**GOOD["urgency"], "score": -0.5}),
    "confidence below zero": replaced(product_line={**GOOD["product_line"], "confidence": -0.1}),
    "noul as a boolean": replaced(domain_relevance={"type": "noul", "noul": True}),
    "a probability above one": replaced(
        product_line={**GOOD["product_line"], "probabilities": {"banking": 1.5, "insurance": 0.2}}
    ),
    "a probability below zero": replaced(
        product_line={**GOOD["product_line"], "probabilities": {"banking": -0.2, "insurance": 1.2}}
    ),
    "a probability as text": replaced(
        product_line={**GOOD["product_line"], "probabilities": {"banking": "0.8"}}
    ),
    "empty probabilities": replaced(product_line={**GOOD["product_line"], "probabilities": {}}),
    "probabilities is a list": replaced(
        product_line={**GOOD["product_line"], "probabilities": [0.8, 0.2]}
    ),
}


@pytest.mark.parametrize("name", sorted(BAD_DECISIONS))
def test_a_bad_decision_payload_is_a_typed_surprise_never_a_value(name):
    with pytest.raises(SchemaSurprise):
        parse_answers(BAD_DECISIONS[name], QUESTIONS)


BAD_ENVELOPES = {
    "no answers key": {"id": "x", "usage": {"input_tokens": 1}},
    "answers is a list": {"answers": [], "usage": {}},
    "usage is a list": {"answers": GOOD, "usage": [1, 2]},
    "negative tokens": {"answers": GOOD, "usage": {"input_tokens": -1, "output_tokens": 1}},
    "token count is text": {"answers": GOOD, "usage": {"input_tokens": "many"}},
    "cost is infinite": {"answers": GOOD, "usage": {"cost": float("inf")}},
    "cost is negative": {"answers": GOOD, "usage": {"cost": -0.5}},
}


@pytest.mark.parametrize("name", sorted(BAD_ENVELOPES))
def test_a_bad_response_envelope_is_a_typed_surprise(name):
    with pytest.raises(SchemaSurprise):
        read_jev_response(BAD_ENVELOPES[name], QUESTIONS)


def test_a_good_envelope_reads_snapshot_provider_tokens_and_cost():
    data = {
        "answers": GOOD,
        "model": "typesafe/jev-1.13-20260917",
        "provider": "TypeSafe",
        "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.00002},
    }
    read = read_jev_response(data, QUESTIONS)
    assert read["input_tokens"] == 476 and read["output_tokens"] == 70
    assert read["snapshot"].startswith("typesafe/jev-1.13") and math.isfinite(read["cost"])


# ====================================================== a chat model asked for the same JSON
@pytest.mark.parametrize(
    "text",
    [
        json.dumps(GOOD),
        "```json\n" + json.dumps(GOOD) + "\n```",
        "Here is my answer: " + json.dumps(GOOD) + " Hope that helps.",
    ],
)
def test_the_fallback_reader_accepts_fenced_json_and_json_inside_prose(text):
    assert set(parse_fallback_answers(text, QUESTIONS)) == {q.id for q in QUESTIONS}


def test_the_fallback_reader_derives_a_missing_confidence_from_the_probabilities():
    data = json.loads(json.dumps(GOOD))
    del data["product_line"]["confidence"]
    parsed = parse_fallback_answers(json.dumps(data), QUESTIONS)
    assert parsed["product_line"].confidence == pytest.approx(0.8)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "I cannot help with that.",
        "{",
        '{"domain_relevance": ',
        "[1, 2, 3]",
        json.dumps({"domain_relevance": "yes"}),
        json.dumps({**GOOD, "domain_relevance": {"noul": "high"}}),
        '{"a": ' * 500,  # absurdly deep nesting
        json.dumps({**GOOD, "product_line": {"choice": ["x"], "probabilities": 3}}),
    ],
)
def test_unreadable_model_output_is_an_invalid_output_error_only(text):
    with pytest.raises(InvalidOutput):
        parse_fallback_answers(text, QUESTIONS)


def test_extract_json_takes_the_first_complete_object():
    assert extract_json('noise {"a": {"b": 1}} {"c": 2}') == {"a": {"b": 1}}
    with pytest.raises(InvalidOutput):
        extract_json("no braces at all")


# ======================================================================= the generator's answer
def test_a_good_generator_answer_is_normalised():
    answer, error = parse_answer(
        '```json\n{"answer": " The period is 30 days. ", "citations": '
        '[{"chunk_id": "c1"}, "c2", {"chunk_id": "c1"}]}\n```'
    )
    assert error is None
    assert answer.text == "The period is 30 days." and answer.citations == ("c1", "c2")


BAD_ANSWERS = {
    "not json": ("this is prose", "not_json"),
    "an array": ("[1]", "not_an_object"),
    "no answer": ('{"citations": []}', "no_answer"),
    "empty answer": ('{"answer": "   ", "citations": []}', "no_answer"),
    "answer is a number": ('{"answer": 7}', "no_answer"),
    "answer too long": (
        json.dumps({"answer": "x" * (MAX_ANSWER_CHARACTERS + 1), "citations": []}),
        "answer_too_long",
    ),
    "citations not a list": ('{"answer": "a", "citations": "c1"}', "bad_citations"),
    "too many citations": (
        json.dumps({"answer": "a", "citations": [f"c{i}" for i in range(MAX_CITATIONS + 1)]}),
        "bad_citations",
    ),
    "a citation that is a number": ('{"answer": "a", "citations": [3]}', "bad_citations"),
    "a citation that is empty": ('{"answer": "a", "citations": [""]}', "bad_citations"),
    "a citation object without id": ('{"answer": "a", "citations": [{"x": 1}]}', "bad_citations"),
}


@pytest.mark.parametrize("name", sorted(BAD_ANSWERS))
def test_a_bad_generator_answer_is_refused_with_a_short_code(name):
    raw, code = BAD_ANSWERS[name]
    answer, error = parse_answer(raw)
    assert answer is None and error == code


# ============================================================================= embeddings
def unit(size=DIMENSIONS):
    return [1.0] + [0.0] * (size - 1)


@pytest.mark.parametrize(
    ("vectors", "count"),
    [
        ([unit()], 1),
        ([unit(), unit()], 2),
    ],
)
def test_good_embedding_vectors_pass(vectors, count):
    assert vectors_ok(vectors, count)


@pytest.mark.parametrize(
    ("vectors", "count"),
    [
        ([], 1),
        ([unit()], 2),
        ([unit(DIMENSIONS - 1)], 1),
        ([unit(DIMENSIONS + 1)], 1),
        ([[0.0] * DIMENSIONS], 1),
        ([[float("nan")] + [0.0] * (DIMENSIONS - 1)], 1),
        ([[float("inf")] + [0.0] * (DIMENSIONS - 1)], 1),
        ([[None] + [0.0] * (DIMENSIONS - 1)], 1),  # None inside a vector
        ([["0.5"] + [0.0] * (DIMENSIONS - 1)], 1),  # a number as text
        ([[True] + [0.0] * (DIMENSIONS - 1)], 1),
        (["not a vector"], 1),
        ([None], 1),
    ],
)
def test_bad_embedding_vectors_never_pass(vectors, count):
    assert not vectors_ok(vectors, count)


# ================================================================================== rerank
def rerank(body, status=200, n_docs=3):
    def handler(request):
        # raw bytes: a provider can send NaN or Infinity, which httpx's json= refuses to encode
        return httpx.Response(
            status,
            content=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )

    return asyncio.run(
        http_rerank(
            "cohere",
            model="rerank-v3.5",
            query="q",
            documents=[f"d{i}" for i in range(n_docs)],
            top_n=None,
            api_key="k" * 12,
            timeout=5.0,
            transport=httpx.MockTransport(handler),
        )
    )


def test_a_good_rerank_response_is_sorted_best_first_with_integer_indexes():
    items = rerank({"results": [{"index": 2, "relevance_score": 0.1},
                                {"index": 0, "relevance_score": 0.9}]})  # fmt: skip
    assert [i.index for i in items] == [0, 2] and all(isinstance(i.index, int) for i in items)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"results": "none"},
        {"results": [{"index": 0}]},
        {"results": [{"index": "x", "relevance_score": 0.5}]},
        {"results": [{"index": 5, "relevance_score": 0.5}]},
        {"results": [{"index": -1, "relevance_score": 0.5}]},
        {"results": [{"index": 0, "relevance_score": "high"}]},
        {"results": [None]},
        ["not", "an", "object"],
        {"results": []},  # three passages asked, nothing ranked
        {"results": {"0": 0.5}},
        {"results": [{"index": "1", "relevance_score": 0.5}]},  # an index as text
        {"results": [{"index": 1.0, "relevance_score": 0.5}]},  # an index as a float
        {"results": [{"index": True, "relevance_score": 0.5}]},  # an index as a boolean
        {"results": [{"index": 0, "relevance_score": "0.9"}]},  # a score as text
        {"results": [{"index": 0, "relevance_score": True}]},
        {"results": [{"index": 0, "relevance_score": float("nan")}]},
        {"results": [{"index": 0, "relevance_score": float("inf")}]},
        {"results": [{"index": 0, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.4}]},
    ],
)
def test_a_bad_rerank_response_is_a_typed_surprise(body):
    with pytest.raises(GatewayError) as caught:
        rerank(body)
    assert isinstance(caught.value, SchemaSurprise)


def test_extra_fields_and_extra_answers_are_ignored_and_never_reach_the_result():
    noisy = {
        **GOOD,
        "an_unrequested_question": {"type": "noul", "noul": 0.1},
        "domain_relevance": {"type": "noul", "noul": 0.9, "reasoning": "a note from the model"},
    }
    parsed = parse_answers(noisy, QUESTIONS)
    assert set(parsed) == {q.id for q in QUESTIONS}
    assert parsed["domain_relevance"] == NoulAnswer(0.9)
    assert "reasoning" not in repr(parsed)


# ==== a deeply nested payload that really reaches the recursion path ====
def test_a_closed_deeply_nested_payload_is_an_invalid_output_not_a_crash():
    depth = 5_000
    text = '{"a": ' * depth + "1" + "}" * depth  # complete, so extract_json hands it to json
    with pytest.raises(InvalidOutput):
        extract_json(text)
    with pytest.raises(InvalidOutput):
        parse_fallback_answers(text, QUESTIONS)
    answer, error = parse_answer('{"answer": ' * depth + '"x"' + "}" * depth)
    assert answer is None and error == "not_json"


# ==== a 200 that is not JSON, at the HTTP clients ====
def test_a_200_with_a_non_json_body_is_a_typed_surprise_for_rerank_and_jev():
    def transport(body):
        return httpx.MockTransport(lambda request: httpx.Response(200, content=body))

    for body in (b"", b"<html>gateway error</html>", b"not json"):
        with pytest.raises(SchemaSurprise):
            asyncio.run(
                http_rerank(
                    "cohere",
                    model="m",
                    query="q",
                    documents=["a", "b"],
                    top_n=None,
                    api_key="k" * 12,
                    timeout=5.0,
                    transport=transport(body),
                )
            )
        client = OpenRouterDecisionsClient(transport=transport(body))
        with pytest.raises(SchemaSurprise):
            asyncio.run(
                client.decide(model="m", state={}, questions={}, api_key="k" * 12, timeout=5.0)
            )


def test_a_bad_vector_from_a_provider_fails_over_instead_of_being_an_internal_error():
    """Through the gateway, not only the checker: key A returns a vector with None in it, key B
    is asked next and answers."""
    from app.gateway.backend import RawEmbedding
    from tests.helpers import FakeBackend, make_gateway

    good = [1.0] + [0.0] * (DIMENSIONS - 1)
    bad = [None] + [0.0] * (DIMENSIONS - 1)
    backend = FakeBackend()
    backend.embed_handler = lambda model, key, inputs, n: RawEmbedding(
        [bad if n == 1 else good for _ in inputs], 3, 0.0001, "litellm"
    )
    result = asyncio.run(make_gateway(backend=backend).embed(["a clause"]))
    assert len(result.vectors) == 1 and len(backend.embed_calls) == 2
    assert vectors_ok(result.vectors, 1)
