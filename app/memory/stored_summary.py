"""The summary text that may be saved (spec 8.2, 9.2).

A `StoredSummary` is made only by `app.memory.summary.fold_summary`, after the model's reply has
been turned into plain text, masked again, scanned for instruction-like text and clipped.
`save_summary` accepts nothing else, so no other code path can put an unchecked string into
`sessions.summary_text`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from app.guardrails.minted import MintedText


@dataclass(frozen=True)
class StoredSummary(MintedText):
    refusal: ClassVar[str] = "a StoredSummary can only be made by fold_summary"
