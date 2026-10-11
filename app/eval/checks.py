"""Cheap code checks (spec 15.3 "cheap checks"): rules, never a model call.

Light on purpose: the hosted API imports this module for the 5 % production sample, so it holds
only text comparisons and the output guard's own figure functions. Facts about an answer are
compared after one normalisation, so "2.00 per cent", "2.0%" and "2 %" are the same figure and
"1,25,000" equals "125000".
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from app.guardrails.output import extract_figures, figure_is_backed

_COMMA_IN_NUMBER = re.compile(r"(?<=\d),(?=\d)")
_TRAILING_ZEROS = re.compile(r"(\d+\.\d*?)0+(?![\d])")
_BARE_DOT = re.compile(r"(\d+)\.(?!\d)")
_PERCENT = re.compile(r"\s*%")
_SPACES = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Lower case, one space between words, `%` as "per cent", digit groups and trailing zeros of
    a decimal removed (2.40 -> 2.4, 30.00 -> 30, 1,25,000 -> 125000)."""
    value = (text or "").lower().replace(" ", " ")
    value = _PERCENT.sub(" per cent", value)
    value = _COMMA_IN_NUMBER.sub("", value)
    value = _TRAILING_ZEROS.sub(r"\1", value)
    value = _BARE_DOT.sub(r"\1", value)
    return _SPACES.sub(" ", value).strip()


def contains(text: str, phrase: str) -> bool:
    needle = normalise(phrase)
    return bool(needle) and needle in normalise(text)


def facts_present(answer: str, facts: Sequence[str]) -> tuple[int, int]:
    """(facts found in the answer, facts asked for)."""
    return sum(1 for fact in facts if contains(answer, fact)), len(facts)


def forbidden_found(answer: str, forbidden: Iterable[str]) -> list[int]:
    """Positions (in the given list) of the forbidden strings that appear in the answer. The
    strings themselves are never returned: a leak is reported by its position only."""
    return [i for i, text in enumerate(forbidden) if contains(answer, text)]


def source_hits(contexts: Sequence[str], anchors: Sequence[str]) -> tuple[int, int]:
    """(expected anchors found in some context, expected anchors)."""
    return sum(1 for a in anchors if any(contains(ctx, a) for ctx in contexts)), len(anchors)


def anchored_precision(contexts: Sequence[str], anchors: Sequence[str]) -> float | None:
    """Share of the contexts that hold an expected anchor (None when there are no contexts)."""
    if not contexts:
        return None
    good = sum(1 for ctx in contexts if any(contains(ctx, a) for a in anchors))
    return good / len(contexts)


def numeric_faithfulness(answer: str, contexts: Sequence[str]) -> float | None:
    """Share of the figures (numbers, percentages, dates, amounts) in the answer that appear in
    the contexts. None when the answer holds no figure (nothing to check)."""
    figures = extract_figures(answer or "")
    if not figures:
        return None
    evidence: set[str] = set()
    for context in contexts:
        for figure in extract_figures(context):
            evidence.add(figure.key)
    backed = sum(1 for figure in figures if figure_is_backed(figure, evidence))
    return backed / len(figures)


def prompt_leak(answer: str, prompt_lines: Iterable[str], *, min_words: int = 8) -> bool:
    """True when the answer repeats a whole line of the system prompts (`min_words` or more)."""
    shown = normalise(answer)
    for line in prompt_lines:
        text = normalise(line)
        if len(text.split()) >= min_words and text in shown:
            return True
    return False


def cheap_checks(result: Any, verdict: Any = None) -> dict[str, Any]:
    """Facts of one finished request, from its result and (when there was one) its output verdict.
    Numbers, flags and names of kinds only: no text."""
    checks: dict[str, Any] = {
        "kind": str(result.kind),
        "reason": str(result.reason) if result.reason else None,
        "cited": len(result.citations or []),
        "warnings": len(result.warnings or []),
        "low_confidence": result.confidence == "low",
        "degraded": len(result.degraded or []),
        "cached": bool(result.cached),
        "disclaimer": bool(result.disclaimer),
        "answer_chars": len(result.text or ""),
    }
    if verdict is not None:
        checks["schema_ok"] = bool(getattr(verdict, "schema_ok", False))
        checks["citation_ok"] = bool(getattr(verdict, "citation_ok", False))
        checks["numeric_ok"] = bool(getattr(verdict, "numeric_ok", False))
        checks["grounded"] = bool(getattr(verdict, "grounded", False))
        checks["coverage"] = round(float(getattr(verdict, "coverage", 0.0)), 4)
    return checks
