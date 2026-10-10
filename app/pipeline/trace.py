"""Per-stage spans and structured errors (spec 4 "structured error", 14 "typed observations per
stage").

A `Span` records one stage: its name, how long it took, how it ended and, for a model call, the
decider, model, provider, key FINGERPRINT and token counts. It never holds query or answer text:
extra values must be numbers, booleans or short identifier-like strings (anything else is dropped
and counted), and the request's raw text has no place in a span at all. Phase 12 connects Langfuse
to the `on_span` sink; the audit-grade fields (prompt hashes, corpus version, chunk ids with content
hashes, filters applied, cache status) are collected in `Trace.audit` the same way.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_SAFE = re.compile(r"^[A-Za-z0-9 _.:/()§,+-]{0,80}$")  # ids, hashes, clause references, names
_MAX_LIST = 50


class StageError(Exception):
    """A stage failed. `code` is a short fixed word; there is never text of the question in it."""

    def __init__(self, stage: str, code: str) -> None:
        super().__init__(f"{stage}: {code}")
        self.stage = stage
        self.code = code


def safe_value(value: Any) -> Any:
    """The value if it is safe to record, else None. Numbers and booleans pass; a string passes
    only if it looks like an id, a hash, a clause reference or a name (never a sentence)."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        words = len(stripped.split())
        if _SAFE.match(stripped) and (words <= 1 or (words <= 3 and len(stripped) <= 60)):
            return stripped
        return None
    if isinstance(value, (list, tuple)):
        items = [safe_value(v) for v in value[:_MAX_LIST]]
        return (
            items
            if all(i is not None or v is None for i, v in zip(items, value, strict=False))
            else None
        )
    return None


@dataclass
class Span:
    stage: str
    started_ms: float
    duration_ms: float = 0.0
    status: str = "ok"  # ok | skipped | error | timeout
    error_code: str | None = None
    decider: str | None = None
    model: str | None = None
    provider: str | None = None
    key_fingerprint: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_billed: float | None = None
    shadow_cost: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def add(self, **fields: Any) -> int:
        """Record extra fields; returns how many were dropped as unsafe."""
        dropped = 0
        for name, value in fields.items():
            cleaned = safe_value(value)
            if cleaned is None and value is not None:
                dropped += 1
            else:
                self.extra[name] = cleaned
        return dropped


@dataclass
class Trace:
    correlation_id: str
    spans: list[Span] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)  # audit-grade fields: ids, hashes, counts
    on_span: Callable[[Span], None] | None = None
    started_at: Any = None  # the wall-clock time the request began (for the span times)

    def finish(self, span: Span) -> None:
        self.spans.append(span)
        if self.on_span:
            try:
                self.on_span(span)
            except Exception:  # a broken sink must never break a request
                pass

    def record(self, **fields: Any) -> None:
        for name, value in fields.items():
            cleaned = safe_value(value)
            if cleaned is not None or value is None:
                self.audit[name] = cleaned

    def timings_ms(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for span in self.spans:
            out[span.stage] = round(out.get(span.stage, 0.0) + span.duration_ms, 1)
        return out

    def total_tokens(self) -> int:
        return sum(s.input_tokens + s.output_tokens for s in self.spans)
