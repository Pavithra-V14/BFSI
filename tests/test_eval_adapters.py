"""The RAGAS and DeepEval adapters (spec 15.3: "their model calls go through the gateway via a
custom LLM wrapper"). They need the evaluation packages (requirements-eval.txt), which are not
installed with the API or the dev tools: without them these tests are skipped. The owner's
evaluation machine, and a throw-away environment of requirements-eval.txt, run them.

No provider is called: `ask` and `embed` are scripted functions, the same seam the gateway fills."""

import asyncio
import json
import os

import pytest

ragas = pytest.importorskip("ragas")
deepeval = pytest.importorskip("deepeval")

from app.eval import deepeval_adapter as da  # noqa: E402
from app.eval import ragas_adapter as ra  # noqa: E402


class Calls:
    def __init__(self):
        self.prompts = []
        self.embedded = []


def run(coro):
    return asyncio.run(coro)


def scripted(calls: Calls, answer=None):
    async def ask(prompt: str) -> str:
        calls.prompts.append(prompt)
        low = prompt.lower()
        if answer is not None:
            return answer
        if "statements" in low and "verdict" in low:
            return json.dumps({"statements": [{"statement": "s", "reason": "r", "verdict": 1}]})
        if "break down" in low or "statements" in low:
            return json.dumps({"statements": ["The penal charge is 2 per cent."]})
        return json.dumps({"verdict": 1, "reason": "ok"})

    return ask


async def embed(texts):
    return [[1.0, 0.0, float(len(t) % 3)] for t in texts]


def score(ask, metrics=None, reference="2 per cent"):
    return run(
        ra.score_sample(
            question="What penal charge applies?",
            answer="The penal charge is 2 per cent.",
            contexts=["Penal charges of 2 per cent apply."],
            reference=reference,
            llm=ra.GatewayLLM(ask),
            embeddings=ra.GatewayEmbeddings(embed),
            metrics=metrics,
        )
    )


# ===================================================================================== RAGAS ====
def test_faithfulness_is_scored_through_the_wrapper_only():
    calls = Calls()
    result = score(scripted(calls), metrics=["faithfulness"])
    assert result == {"faithfulness": 1.0}
    assert len(calls.prompts) == 2  # statement generation, then verdicts


def test_every_prompt_ragas_makes_goes_to_the_function_we_gave_it():
    calls = Calls()
    score(scripted(calls), metrics=["faithfulness", "context_recall"])
    assert calls.prompts and all(isinstance(p, str) for p in calls.prompts)


def test_answer_relevancy_asks_the_judge_for_one_completion_not_three():
    """RAGAS would ask for 3 completions of one prompt; our judge returns one, so the metric is
    set to 1 on purpose: no warning and no hidden extra requests."""
    assert ra.STRICTNESS == 1
    metric = ra._metric("answer_relevancy", ra.GatewayLLM(scripted(Calls())), None)
    assert metric.strictness == 1


def test_answer_correctness_is_scored_through_the_wrapper():
    """RAGAS builds the similarity part of this metric in `init()`, which the single-sample path
    never calls; it raised "AnswerSimilarity must be set" for every case."""

    async def ask(prompt: str) -> str:
        low = prompt.lower()
        if "tp" in low and "fp" in low:
            return json.dumps({"TP": [{"statement": "s", "reason": "r"}], "FP": [], "FN": []})
        return json.dumps({"statements": ["The penal charge is 2 per cent."]})

    result = score(ask, metrics=["answer_correctness"])
    assert result["answer_correctness"] is not None and result["answer_correctness"] > 0.5


def test_a_metric_that_needs_a_reference_is_skipped_when_there_is_none():
    calls = Calls()
    result = score(scripted(calls), metrics=["faithfulness", "context_recall"], reference=None)
    assert set(result) == {"faithfulness"}


def test_an_unreadable_judge_output_is_not_scored_never_zero():
    calls = Calls()
    result = score(scripted(calls, answer="this is not json at all"), metrics=["faithfulness"])
    assert result == {"faithfulness": None}


def test_a_judge_that_raises_is_not_scored():
    async def down(prompt):
        raise RuntimeError("the judge is down")

    assert score(down, metrics=["faithfulness"]) == {"faithfulness": None}


class Stop(BaseException):
    """What the cost guard raises: a BaseException, so no library handler swallows it."""


def test_a_cost_guard_stop_goes_straight_through_the_library():
    async def stop(prompt):
        raise Stop()

    with pytest.raises(Stop):
        score(stop, metrics=["faithfulness"])


def test_one_retry_at_most_so_no_hidden_second_paid_call():
    llm = ra.GatewayLLM(scripted(Calls()))
    assert llm.run_config.max_retries == 1 and llm.multiple_completion_supported is False


def test_the_sync_paths_are_not_available():
    llm = ra.GatewayLLM(scripted(Calls()))
    with pytest.raises(NotImplementedError):
        llm.generate_text(None)


def test_the_embedding_step_ragas_runs_synchronously_works_inside_a_running_loop():
    """RAGAS's answer relevancy embeds in a plain (sync) call made inside the event loop. It used
    to raise, so the metric was never scored."""

    async def inside_a_loop():
        embeddings = ra.GatewayEmbeddings(embed)
        return embeddings.embed_query("abc"), embeddings.embed_documents(["a", "bb"])

    one, many = run(inside_a_loop())
    assert one[2] == 0.0 and len(many) == 2 and many[1][2] == 2.0


def test_answer_relevancy_is_scored_end_to_end_through_the_wrapper():
    calls = Calls()

    async def ask(prompt: str) -> str:
        calls.prompts.append(prompt)
        return json.dumps({"question": "What penal charge applies?", "noncommittal": 0})

    result = score(ask, metrics=["answer_relevancy"], reference=None)
    assert result["answer_relevancy"] is not None
    assert len(calls.prompts) == 1  # strictness 1: one judge request for this metric


def test_embeddings_go_through_the_function_we_gave():
    vectors = run(ra.GatewayEmbeddings(embed).aembed_documents(["a", "bb"]))
    assert len(vectors) == 2 and vectors[1][2] == 2.0


def test_the_usage_report_is_switched_off_before_the_import():
    assert os.environ.get("RAGAS_DO_NOT_TRACK") in ("true", "True", "1")


def test_only_the_named_metrics_exist_and_none_is_multi_modal():
    assert set(ra.METRICS) == {
        "faithfulness", "answer_relevancy", "context_precision", "context_recall",
        "answer_correctness",
    }  # fmt: skip
    assert "context_entity_recall" not in ra.METRICS  # removed by the owner on 2026-10-21
    assert not any("multi_modal" in module for module, *_ in ra.METRICS.values())
    # (ragas itself imports that module when it loads; the advisory concerns calling it, which
    # nothing here does: no metric of ours is built from it)
    assert not any("MultiModal" in cls for _module, cls, *_ in ra.METRICS.values())


# ================================================================================ DeepEval ====
def judge_answering(payload):
    async def ask(prompt: str) -> str:
        return json.dumps(payload)

    return da.GatewayJudge(ask)


def test_a_refusal_is_scored_by_g_eval_through_the_wrapper():
    judge = judge_answering({"steps": ["be polite", "no facts"], "score": 8, "reason": "fine"})
    value = run(da.refusal_quality("q", "I cannot help with that.", judge))
    assert value == pytest.approx(0.8)


def test_answer_equivalence_uses_the_reference():
    judge = judge_answering({"steps": ["compare"], "score": 9, "reason": "same"})
    value = run(da.answer_equivalence("q", "2 per cent", "2 per cent", judge))
    assert value == pytest.approx(0.9)


def test_an_unreadable_g_eval_output_is_not_scored():
    async def ask(prompt):
        return "no json here"

    assert run(da.refusal_quality("q", "no", da.GatewayJudge(ask))) is None


def test_telemetry_is_switched_off_before_the_import():
    assert os.environ.get("DEEPEVAL_TELEMETRY_OPT_OUT") == "YES"
