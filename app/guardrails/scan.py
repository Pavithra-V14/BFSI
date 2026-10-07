"""The local scan: stage 2 of the pipeline (spec 4.1 step 2). No LLM, no network.

In the spec's order: (1) query limits, (2) greeting check, (3) injection patterns, (4) PII masking
of the copy that goes to any model. The first two can end the request here with a fixed text; the
injection regex only scans (stage 4 decides, after the Jev score is known).

The result carries what the trace needs (token count, the pattern NAMES that matched, mask counts,
the time each step took) and never the query text except the MASKED copy.
"""

from __future__ import annotations

import time
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from app.guardrails import messages
from app.guardrails.greeting import classify_greeting, greeting_reply
from app.guardrails.injection import InjectionScan, scan_query
from app.guardrails.limits import MAX_QUERY_TOKENS, MIN_FIRST_MESSAGE_CHARACTERS, check_limits
from app.guardrails.masking import Masker


@dataclass
class ScanResult:
    action: str  # "continue" | "refuse" | "reply"
    reason: str | None = None  # empty | too_short | too_long | greeting | farewell | thanks
    message: str | None = None  # the fixed text for "refuse" and "reply"
    tokens: int = 0
    approximate_tokens: bool = False
    injection: InjectionScan = field(default_factory=InjectionScan)
    masked: str | None = None  # the query copy that may go to a model
    mask_counts: dict[str, int] = field(default_factory=dict)
    allowlisted: int = 0
    timings_ms: dict[str, float] = field(default_factory=dict)


def local_scan(
    query: str,
    *,
    first_message: bool,
    masker: Masker,
    counter: Any,
    allow_terms: Collection[str] = (),
) -> ScanResult:
    timings: dict[str, float] = {}
    clock = time.perf_counter

    started = clock()
    limits = check_limits(query, first_message=first_message, counter=counter)
    timings["limits"] = (clock() - started) * 1000
    if not limits.ok:
        text = {
            "empty": messages.EMPTY_QUERY,
            "too_short": messages.TOO_SHORT.format(minimum=MIN_FIRST_MESSAGE_CHARACTERS),
            "too_long": messages.TOO_LONG.format(limit=MAX_QUERY_TOKENS),
        }[limits.reason or "empty"]
        return ScanResult(
            "refuse", limits.reason, text, limits.tokens, limits.approximate, timings_ms=timings
        )

    started = clock()
    kind = classify_greeting(query)
    timings["greeting"] = (clock() - started) * 1000
    if kind is not None:
        return ScanResult(
            "reply",
            kind,
            greeting_reply(kind),
            limits.tokens,
            limits.approximate,
            timings_ms=timings,
        )

    started = clock()
    injection = scan_query(query)
    timings["injection"] = (clock() - started) * 1000

    started = clock()
    masked = masker.mask(query.strip(), allow_terms)
    timings["masking"] = (clock() - started) * 1000
    return ScanResult(
        "continue",
        None,
        None,
        limits.tokens,
        limits.approximate,
        injection,
        masked.text,
        dict(masked.counts),
        masked.kept,
        timings,
    )
