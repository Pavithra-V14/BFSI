"""The conditional disclaimer (spec 9.5; "only when relevant" was open item 23, owner-decided).

The standard disclaimer is shown only when it matters:
- the question is advice-like ("should I", "recommend", "which is better", "invest in" ...), or
- the answer contains a financial figure (an amount, a rate or a percentage).
A plain definition ("what is KYC?") gets none. The text is `messages.DISCLAIMER` [T].
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.guardrails import messages
from app.guardrails.output import has_financial_figure

_ADVICE = re.compile(
    r"\b(should (?:i|we)\b|what should\b|recommend\w*|advis\w+|advice|suggest\w*|"
    r"which (?:\w+ ){0,3}is (?:better|best)|better (?:option|plan|policy|fund|scheme)|"
    r"best (?:option|plan|policy|fund|scheme|bank)|worth (?:it|buying|investing)|"
    r"invest(?:ing|ment)? in|is it (?:good|safe|wise) to)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DisclaimerDecision:
    show: bool
    reasons: tuple[str, ...] = ()  # "advice_question" | "financial_figure"


def disclaimer_decision(question: str, answer: str) -> DisclaimerDecision:
    reasons = []
    if _ADVICE.search(question):
        reasons.append("advice_question")
    if has_financial_figure(answer):
        reasons.append("financial_figure")
    return DisclaimerDecision(bool(reasons), tuple(reasons))


def with_disclaimer(answer: str, decision: DisclaimerDecision) -> str:
    return f"{answer}\n\n{messages.DISCLAIMER}" if decision.show else answer
