"""Scoring one finished item with the judge: what rules cannot see (spec 15.3, 15.4a).

For a case (expected behaviour and reference known) and for a production sample (nothing known
but the question, the answer and the passages). Every score is a `Score`; a value that nobody
could produce is `None` ("not scored"). The functions here make no call themselves: they ask the
`Judge`, which goes through the gateway under the cost guard.

Which library does what:
- RAGAS: faithfulness, answer relevancy, context precision / recall / entity recall, answer
  correctness (the RAG set of 15.4a);
- typed verdicts (Jev when calibration trusts the metric, else the judge model): relevance of each
  passage, support of each citation, correctness of a refusal;
- DeepEval's G-Eval: wording judgements (a refusal is polite and invents nothing; an answer is
  equivalent to its reference).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.eval.cases import Case
from app.eval.judge import Judge, JudgeUnavailable
from app.eval.metrics import Score, mean

MAX_PASSAGES = 5


def _share(values: dict[str, bool | None]) -> float | None:
    known = [v for v in values.values() if v is not None]
    return (sum(1 for v in known if v) / len(known)) if known else None


def _not_scored(names: Sequence[str], reason: str) -> list[Score]:
    return [Score(name, None, decider="not_scored", detail=reason) for name in names]


async def judge_item(
    item: dict[str, Any],
    *,
    question: str,
    judge: Judge,
    case: Case | None = None,
    use_ragas: bool = True,
    use_deepeval: bool = True,
) -> list[Score]:
    """The judged scores of one item. `item` is a stored run item (or the same shape built from a
    production sample)."""
    kind = item.get("answer_kind") or ""
    answer = item.get("answer_text") or ""
    contexts: list[str] = list(item.get("contexts") or [])[:MAX_PASSAGES]
    models = item.get("models") or []
    reference = case.reference if case and case.reference else None
    scores: list[Score] = []
    answered = kind == "answer" and bool(answer)

    judged_names = []
    if answered:
        judged_names += ["faithfulness", "answer_relevancy", "context_relevance"]
        if contexts and item.get("cheap_checks", {}).get("cited_idx"):
            judged_names.append("citation_accuracy")
    if kind == "refusal":
        judged_names.append("refusal_correct")
    if not judged_names:
        return []
    if judge.family_clash(models):
        return _not_scored(judged_names, "judge_same_family")
    if not judge.judge_model and not any(judge.trusted.values()):
        return _not_scored(judged_names, "no_judge_model")

    passages = {f"p{i}": text for i, text in enumerate(contexts)}

    if answered and contexts:
        verdict = await judge.verdicts(
            "chunk_relevant", list(passages), {"question": question, "passages": passages}
        )
        scores.append(
            Score("context_relevance", _share(verdict.values), verdict.decider, verdict.reason)
        )
        cited = [
            f"p{i}"
            for i in (item.get("cheap_checks", {}).get("cited_idx") or [])
            if f"p{i}" in passages
        ]
        if cited:
            support = await judge.verdicts(
                "citation_supports",
                cited,
                {"claim": answer, "passages": {k: passages[k] for k in cited}},
            )
            scores.append(
                Score("citation_accuracy", _share(support.values), support.decider, support.reason)
            )
    if kind == "refusal":
        refusal = await judge.verdicts(
            "refusal_correct",
            ["refusal"],
            {"question": question, "answer": answer, "passages": passages},
        )
        value = refusal.values.get("refusal")
        scores.append(
            Score(
                "refusal_correct",
                None if value is None else (1.0 if value else 0.0),
                refusal.decider,
                refusal.reason,
            )
        )

    if answered and contexts and use_ragas and judge.judge_model:
        scores.extend(await _ragas_scores(question, answer, contexts, reference, judge))
    elif answered and use_ragas:
        scores.extend(_not_scored(["faithfulness", "answer_relevancy"], "no_judge_model"))
    if use_deepeval and judge.judge_model and (case is not None):
        scores.extend(await _deepeval_scores(question, answer, kind, case, judge))
    return scores


async def _ragas_scores(
    question: str, answer: str, contexts: list[str], reference: str | None, judge: Judge
) -> list[Score]:
    try:
        from app.eval import ragas_adapter
    except ImportError:  # the evaluation packages are not installed here
        return _not_scored(["faithfulness", "answer_relevancy"], "ragas_not_installed")
    llm = ragas_adapter.GatewayLLM(judge.text)
    embeddings = ragas_adapter.GatewayEmbeddings(judge.embed)
    try:
        raw = await ragas_adapter.score_sample(
            question=question,
            answer=answer,
            contexts=contexts,
            reference=reference,
            llm=llm,
            embeddings=embeddings,
        )
    except JudgeUnavailable:
        return _not_scored(list(ragas_adapter.METRICS), "judge_unavailable")
    model = judge.judge_model or "judge"
    out = [Score(name, value, f"ragas:{model}") for name, value in raw.items()]
    faithful = raw.get("faithfulness")
    if faithful is not None:  # reference-free hallucination rate (15.4a)
        out.append(Score("hallucination_rate", round(1.0 - faithful, 6), f"ragas:{model}"))
    return out


async def _deepeval_scores(
    question: str, answer: str, kind: str, case: Case, judge: Judge
) -> list[Score]:
    try:
        from app.eval import deepeval_adapter
    except ImportError:
        return []
    model = judge.judge_model or "judge"
    gateway_judge = deepeval_adapter.GatewayJudge(judge.text)
    out: list[Score] = []
    try:
        if kind == "refusal" and case.behaviour in ("refuse", "block"):
            value = await deepeval_adapter.refusal_quality(question, answer, gateway_judge)
            out.append(Score("refusal_quality", value, f"deepeval:{model}"))
        elif kind == "answer" and case.behaviour == "answer" and case.reference:
            value = await deepeval_adapter.answer_equivalence(
                question, answer, case.reference, gateway_judge
            )
            out.append(Score("answer_equivalence", value, f"deepeval:{model}"))
    except JudgeUnavailable:
        out.append(Score("deepeval", None, "not_scored", "judge_unavailable"))
    return out


__all__ = ["judge_item", "mean"]
