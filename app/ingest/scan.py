"""The ingestion scan for instruction-like text in a chunk (spec 9.3 "indirect injection").

A document is data, never instructions. A chunk that reads like an instruction to the model
("ignore the previous instructions", "reveal your system prompt", a chat-template marker) is held
back: it is stored but NOT active until the owner reviews it (`review chunks`). The patterns are
deliberately narrow, because a policy document legitimately says things like "you are now
entitled". The context guard (Phase 6) scans retrieved chunks again with its own, wider rules.
"""

from __future__ import annotations

from app.guardrails.injection import DOCUMENT_PATTERNS, scan_document

PATTERNS = DOCUMENT_PATTERNS  # the list lives in the guardrails (one source for ingest and query)


def scan_text(text: str) -> list[str]:
    """The names of the patterns that match (empty for clean text). Names only, never the text."""
    return scan_document(text)
