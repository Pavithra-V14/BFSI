"""RAGAS through our own gateway (spec 15.3: "their model calls go through the gateway via a custom
LLM wrapper"). The only module that imports ragas.

RAGAS never gets a provider key and never makes a network call itself: its LLM is `GatewayLLM`
(every prompt goes to `ask`, which is the judge's text call: evaluation keys, judge model, cost
guard) and its embeddings are `GatewayEmbeddings` (every text goes to `embed`, the gateway's
embedding call). RAGAS's own usage reporting is switched off before the import, its disk cache is
never enabled, and retries are one (a second try would be a second paid call that the cost guard
counts anyway). Only the metrics named in `METRICS` are used; in particular not the multi-modal
ones, whose published advisory concerns reading files and addresses named in the contexts.

A metric that cannot be computed (the judge is down, the output could not be read) is `None`:
"not scored", never zero.
"""

from __future__ import annotations

import math
import os
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from app.eval.notscored import NotScored

os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")  # no usage report leaves this machine

from langchain_core.embeddings import Embeddings  # noqa: E402,F401
from langchain_core.outputs import Generation, LLMResult  # noqa: E402
from ragas.dataset_schema import SingleTurnSample  # noqa: E402
from ragas.embeddings.base import BaseRagasEmbeddings  # noqa: E402
from ragas.llms.base import BaseRagasLLM  # noqa: E402
from ragas.run_config import RunConfig  # noqa: E402

Ask = Callable[[str], Awaitable[str]]
Embed = Callable[[list[str]], Awaitable[list[list[float]]]]

# metric name used in our results -> (module, class, needs reference, needs embeddings)
METRICS: dict[str, tuple[str, str, bool, bool]] = {
    "faithfulness": ("ragas.metrics._faithfulness", "Faithfulness", False, False),
    "answer_relevancy": ("ragas.metrics._answer_relevance", "ResponseRelevancy", False, True),
    "context_precision": (
        "ragas.metrics._context_precision", "LLMContextPrecisionWithReference", True, False,
    ),
    "context_recall": ("ragas.metrics._context_recall", "LLMContextRecall", True, False),
    "context_entity_recall": (
        "ragas.metrics._context_entities_recall", "ContextEntityRecall", True, False,
    ),
    "answer_correctness": ("ragas.metrics._answer_correctness", "AnswerCorrectness", True, True),
}  # fmt: skip
# Reference-free ones, for a production sample (no expected answer exists).
REFERENCE_FREE = ("faithfulness", "answer_relevancy")


class GatewayLLM(BaseRagasLLM):
    """RAGAS's LLM, backed by one async function that asks the judge."""

    def __init__(self, ask: Ask) -> None:
        super().__init__(
            run_config=RunConfig(timeout=120, max_retries=1, max_wait=1, max_workers=1),
            multiple_completion_supported=False,
        )
        self._ask = ask

    async def agenerate_text(
        self, prompt: Any, n: int = 1, temperature: float | None = 0.01,
        stop: list[str] | None = None, callbacks: Any = None,
    ) -> LLMResult:  # fmt: skip
        text = await self._ask(prompt.to_string())
        return LLMResult(generations=[[Generation(text=text)]])

    def generate_text(
        self, prompt: Any, n: int = 1, temperature: float = 0.01,
        stop: list[str] | None = None, callbacks: Any = None,
    ) -> LLMResult:  # fmt: skip
        raise NotImplementedError("the evaluation scores asynchronously")

    def is_finished(self, response: LLMResult) -> bool:
        return True


class GatewayEmbeddings(BaseRagasEmbeddings):
    def __init__(self, embed: Embed) -> None:
        super().__init__()
        self._embed = embed
        self.run_config = RunConfig(max_retries=1, max_wait=1, max_workers=1)

    def embed_query(self, text: str) -> list[float]:
        raise NotImplementedError("the evaluation scores asynchronously")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("the evaluation scores asynchronously")

    async def aembed_query(self, text: str) -> list[float]:
        return (await self._embed([text]))[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(list(texts))


def _metric(name: str, llm: BaseRagasLLM, embeddings: BaseRagasEmbeddings) -> Any:
    import importlib

    module, cls, _reference, wants_embeddings = METRICS[name]
    metric = getattr(importlib.import_module(module), cls)()
    metric.llm = llm
    if wants_embeddings:
        metric.embeddings = embeddings
    return metric


async def score_sample(
    *,
    question: str,
    answer: str,
    contexts: Sequence[str],
    reference: str | None,
    llm: GatewayLLM,
    embeddings: GatewayEmbeddings,
    metrics: Sequence[str] | None = None,
    reasons: dict[str, str] | None = None,
) -> dict[str, float | None]:
    """The named metrics for one answered question. A metric that needs a reference is skipped
    (absent from the result) when none is given. When `reasons` is given, a metric that could not
    be computed gets its reason there (the judge's own reason when it was a judge outage)."""
    wanted = list(metrics or METRICS)
    sample = SingleTurnSample(
        user_input=question,
        response=answer,
        retrieved_contexts=list(contexts),
        reference=reference,
    )
    out: dict[str, float | None] = {}
    for name in wanted:
        if name not in METRICS:
            continue
        needs_reference = METRICS[name][2]
        if needs_reference and not reference:
            continue
        try:
            value = await _metric(name, llm, embeddings).single_turn_ascore(sample)
        except NotScored as error:  # the judge is down or unset: its reason is kept
            out[name] = None
            if reasons is not None:
                reasons[name] = error.reason
            continue
        except Exception:  # an unreadable output: "not scored"
            out[name] = None
            if reasons is not None:
                reasons[name] = "unreadable_output"
            continue
        out[name] = (
            None
            if value is None or (isinstance(value, float) and math.isnan(value))
            else float(value)
        )
    return out
