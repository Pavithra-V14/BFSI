"""Stage 3: Jev triage, one combined call on the MASKED question (spec 4.1 step 3, 5.1).

One call, typed questions: domain relevance, needs-history, vague, injection score and (when the
corpus has values) product line and clause type. The gateway tries Jev, then the chat fallbacks;
when it falls to "rules" or fails altogether this module applies its own keyword rules or, if even
that is impossible, the safe default of the owner (Phase 8): the question is treated as IN-DOMAIN,
soft filters are OFF and a missing injection score means the regexes alone decide. A failed call
never blocks a question as out-of-domain.

Every probability is checked before use: a NaN, an infinity or a value outside [0, 1] counts as "not
known" (an event names it; nothing else changes).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.gateway import questions as q
from app.gateway.decisions import ChoiceAnswer, NoulAnswer
from app.pipeline.state import PipelineState, Triage
from app.pipeline.trace import Span

_FOLLOW_UP = re.compile(
    r"^\s*(and|what about|how about|also|then|so)\b|\b(it|that|this|those|these|they|them)\b",
    re.IGNORECASE,
)


def valid_probability(value: Any) -> float | None:
    """The value as a probability, or None when it is not a finite number in [0, 1]."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None


def rules_triage(text: str, first_message: bool) -> Triage:
    """The no-LLM fallback: keywords only. It never says "out of domain" (it cannot know), never
    gives a soft filter and never gives an injection score."""
    words = re.findall(r"[A-Za-z0-9']+", text)
    follow_up = bool(_FOLLOW_UP.search(text)) and len(words) <= 8
    vague = len(words) < 3
    return Triage(
        decider="rules",
        in_domain_probability=None,
        needs_history=1.0 if follow_up else 0.0,
        vague=1.0 if (vague and first_message) else 0.0,
        injection_score=None,
    )


def build_questions(values: Mapping[str, Sequence[str]]) -> list[Any]:
    asked: list[Any] = [
        q.domain_relevance(),
        q.needs_history(),
        q.vague_first_message(),
        q.injection_attempt(),
    ]
    if len(values.get("product_line", ())) >= 2:
        asked.append(q.product_line(list(values["product_line"])))
    if len(values.get("clause_type", ())) >= 2:
        asked.append(q.clause_type(list(values["clause_type"])))
    return asked


def read_answers(answers: Mapping[str, Any], emit: Any) -> Triage:
    def noul(name: str) -> float | None:
        answer = answers.get(name)
        if not isinstance(answer, NoulAnswer):
            return None
        value = valid_probability(answer.probability)
        if value is None:
            emit("triage_probability_invalid", question=name)
        return value

    def choice(name: str) -> tuple[str | None, float | None]:
        answer = answers.get(name)
        if not isinstance(answer, ChoiceAnswer):
            return None, None
        probability = valid_probability(answer.probabilities.get(answer.option))
        if probability is None:
            emit("triage_probability_invalid", question=name)
            return None, None
        return answer.option, probability

    line, line_p = choice("product_line")
    clause, clause_p = choice("clause_type")
    return Triage(
        in_domain_probability=noul("domain_relevance"),
        needs_history=noul("needs_history"),
        vague=noul("vague_first_message"),
        injection_score=noul("injection_attempt"),
        product_line=line,
        product_line_probability=line_p,
        clause_type=clause,
        clause_type_probability=clause_p,
    )


async def run_triage(
    ctx: PipelineState, span: Span, timeout_s: float, values: Mapping[str, Sequence[str]]
) -> Triage:
    """The decisions for this question. Never raises for a provider failure: the owner's default
    applies (in-domain, soft filters off, regex-only injection)."""
    text = ctx.pass1.text if ctx.pass1 else ""
    try:
        result = await ctx.deps.gateway.decide(
            build_questions(values), text, tier="decide", timeout_s=timeout_s
        )
    except Exception as error:  # every deciders failed, or a timeout inside the gateway
        span.status = "error"
        span.error_code = type(error).__name__
        ctx.emit("triage_failed", reason=type(error).__name__)
        return Triage(decider="none", failed=True)
    span.decider = result.decider
    span.model = result.model
    span.provider = result.provider
    span.key_fingerprint = result.key_fingerprint
    span.input_tokens = result.input_tokens
    span.output_tokens = result.output_tokens
    span.cost_billed = result.cost_billed
    span.shadow_cost = result.shadow_cost
    if result.answers is None:  # the gateway fell all the way to "rules"
        return rules_triage(text, ctx.first_message)
    triage = read_answers(result.answers, ctx.emit)
    triage.decider = result.decider
    return triage
