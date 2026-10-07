"""The local scan: stage 2 of the pipeline (spec 4.1 step 2). No LLM, no network.

In the spec's order: (1) query limits, (2) greeting check, (3) injection patterns, (4) PII masking
of the copy that goes to any model. The first two can end the request here with a fixed text; the
injection regex only scans (stage 4 decides, after the Jev score is known). One exception to the
order, decided by the owner: the first-message MINIMUM length runs after the greeting check, so a
first message of "hi" gets the greeting reply and not "too short" (empty and too long still come
first).

Two copies of a question exist, and the types keep them apart:
- `ModelCopy` (pass 1, made here): masked with the role's approved list, for models only. It
  cannot be printed or joined into a string by accident, and nothing stores, logs or caches it;
- `StoredQuery` (pass 2, `mask_for_storage`): made at answer time from the RAW question with only
  the surviving documents' entries. This is the only copy that may be stored, logged or cached.

The result carries what the trace needs (token count, the pattern NAMES that matched, mask counts,
the time each step took) and never the query text except the MASKED copy.
"""

from __future__ import annotations

import time
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, overload

from app.guardrails import messages
from app.guardrails.allowlist import AllowList
from app.guardrails.greeting import classify_greeting, greeting_reply
from app.guardrails.injection import InjectionScan, scan_query
from app.guardrails.limits import (
    MAX_QUERY_TOKENS,
    MIN_FIRST_MESSAGE_CHARACTERS,
    check_limits,
    is_too_short,
)
from app.guardrails.masking import Masker
from app.guardrails.minted import MintedText, mint


@dataclass(frozen=True)
class ModelCopy:
    """Pass 1: the question masked with the role's approved list. For models only (rewrite, the
    Jev check, the embedding, the generator). It is not a string on purpose: formatting or logging
    it raises, so it cannot reach a log, a trace, a cache key or a stored transcript by mistake."""

    text: str

    def __str__(self) -> str:
        raise TypeError("a ModelCopy is for models only: store the StoredQuery (pass 2) instead")

    def __repr__(self) -> str:
        return f"ModelCopy(<{len(self.text)} characters, for models only>)"


@dataclass(frozen=True)
class StoredQuery(MintedText):
    """Pass 2: the question re-masked with ONLY the entries of the documents that survived
    retrieval. The one copy that may be stored, logged or cached. Only `mask_for_storage` can make
    one: calling the class, `dataclasses.replace`, a subclass or a copy are all refused."""

    counts: dict[str, int] = field(default_factory=dict)
    refusal: ClassVar[str] = "a StoredQuery can only be made by mask_for_storage"


@dataclass(frozen=True)
class StoredAnswer(MintedText):
    """The answer text masked the same way as a StoredQuery (the same function makes both), the
    only form of an assistant message that the chat store accepts."""

    counts: dict[str, int] = field(default_factory=dict)
    refusal: ClassVar[str] = "a StoredAnswer can only be made by mask_for_storage"


@overload
def mask_for_storage(
    raw_query: str,
    *,
    masker: Masker,
    allow: AllowList,
    doc_ids: Collection[str],
    kind: Literal["query"] = "query",
) -> StoredQuery: ...
@overload
def mask_for_storage(
    raw_query: str,
    *,
    masker: Masker,
    allow: AllowList,
    doc_ids: Collection[str],
    kind: Literal["answer"],
) -> StoredAnswer: ...
def mask_for_storage(
    raw_query: str,
    *,
    masker: Masker,
    allow: AllowList,
    doc_ids: Collection[str],
    kind: Literal["query", "answer"] = "query",
) -> StoredQuery | StoredAnswer:
    """Pass 2, at answer time. `raw_query` is the question as typed (kept in memory by the caller
    for this purpose only), or with `kind="answer"` the answer text to be stored; `doc_ids` are the
    documents the answer was built from. `None` is refused, because "every document" is the pass-1
    scope, not this one. The chat store accepts only what this function returns."""
    if doc_ids is None:
        raise TypeError("pass 2 needs the ids of the surviving documents, not None")
    if kind not in ("query", "answer"):
        raise ValueError("kind must be 'query' or 'answer'")
    result = masker.mask(raw_query.strip(), allow.terms(set(doc_ids)))
    made = StoredQuery if kind == "query" else StoredAnswer
    return mint(made, text=result.text, counts=dict(result.counts))


@dataclass
class ScanResult:
    action: str  # "continue" | "refuse" | "reply"
    reason: str | None = None  # empty | too_short | too_long | greeting | farewell | thanks
    message: str | None = None  # the fixed text for "refuse" and "reply"
    tokens: int = 0
    approximate_tokens: bool = False
    injection: InjectionScan = field(default_factory=InjectionScan)
    masked: ModelCopy | None = None  # the pass-1 copy: for models only, never stored
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
    limits = check_limits(query, first_message=False, counter=counter)  # empty and too long
    timings["limits"] = (clock() - started) * 1000
    if not limits.ok:
        text = {
            "empty": messages.EMPTY_QUERY,
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

    if is_too_short(query, first_message=first_message):
        text = messages.TOO_SHORT.format(minimum=MIN_FIRST_MESSAGE_CHARACTERS)
        return ScanResult(
            "refuse", "too_short", text, limits.tokens, limits.approximate, timings_ms=timings
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
        ModelCopy(masked.text),
        dict(masked.counts),
        masked.kept,
        timings,
    )
