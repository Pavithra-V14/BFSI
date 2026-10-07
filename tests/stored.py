"""The test-only way to make the masked text types without masking (spec 9.2, CLAUDE.md).

`StoredQuery`, `StoredAnswer` and `StoredSummary` can only be made by their own functions
(`mask_for_storage`, `fold_summary`). Tests that need a message, a summary or a fixed reply in the
store without running the masker use these builders, so the production door stays shut. Nothing
under `app/` may import this module (a test checks).
"""

from __future__ import annotations

from app.guardrails.minted import mint
from app.guardrails.scan import StoredAnswer, StoredQuery
from app.memory.stored_summary import StoredSummary


def stored_query(text: str) -> StoredQuery:
    return mint(StoredQuery, text=text, counts={})


def stored_answer(text: str) -> StoredAnswer:
    return mint(StoredAnswer, text=text, counts={})


def stored_summary(text: str) -> StoredSummary:
    return mint(StoredSummary, text=text)
