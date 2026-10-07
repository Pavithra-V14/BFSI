"""The context guard and the context block (spec 4.1 steps 11 and 13, 8.6, 9.3).

After reranking, each chunk is checked before it can reach the generator:
- a chunk whose relevance score is below the floor is dropped [T 0.30, tuned at the baseline run];
- a chunk whose text reads like an instruction to the model (the wider chunk patterns) is
  dropped and the verdict records which patterns matched (names only), so the owner can see it.
If nothing is left, the pipeline refuses with a fixed text (`messages.NO_RELIABLE_CONTEXT`).

What survives is wrapped in clear delimiters for the prompt, each chunk labelled with a short id
(S1, S2 ...), its source, effective date and clause (8.6 "Clash"). The text inside is neutralised so
a chunk cannot close the delimiter and write its own: angle brackets become single guillemets. The
model is told, in the prompt, that text between the delimiters is data to read, never instructions.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.guardrails.injection import scan_chunk

RELEVANCE_FLOOR = 0.30  # [T]


@dataclass(frozen=True)
class ContextChunk:
    chunk_id: str
    text: str
    score: float
    doc_id: str
    source: str
    effective_date: str
    clause_ref: str | None = None
    heading_path: tuple[str, ...] = ()
    page_start: int | None = None
    page_end: int | None = None
    content_hash: str | None = None


@dataclass(frozen=True)
class Drop:
    chunk_id: str
    reason: str  # "below_floor" | "instruction_like"
    patterns: tuple[str, ...] = ()  # names of the patterns that matched


@dataclass
class ContextVerdict:
    kept: list[ContextChunk] = field(default_factory=list)
    dropped: list[Drop] = field(default_factory=list)

    @property
    def refuse(self) -> bool:
        """Nothing reliable is left to answer from."""
        return not self.kept


def context_guard(
    chunks: Sequence[ContextChunk], *, floor: float = RELEVANCE_FLOOR
) -> ContextVerdict:
    verdict = ContextVerdict()
    for chunk in chunks:
        if chunk.score < floor:
            verdict.dropped.append(Drop(chunk.chunk_id, "below_floor"))
            continue
        hits = scan_chunk(chunk.text)
        if hits:
            verdict.dropped.append(Drop(chunk.chunk_id, "instruction_like", tuple(hits)))
            continue
        verdict.kept.append(chunk)
    return verdict


def neutralise(text: str) -> str:
    """Make text safe to place between the delimiters: no angle bracket can survive, so the text
    cannot close the block or open a new one."""
    return text.replace("<", "‹").replace(">", "›")


_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9 ._,&()/:'-]")


def _label(value: object, limit: int = 80) -> str:
    return " ".join(_LABEL_UNSAFE.sub(" ", str(value)).split())[:limit]


def build_context_block(chunks: Sequence[ContextChunk]) -> tuple[str, dict[str, str]]:
    """The delimited block for the prompt and the map from label ("S1") to chunk id. The label is
    what the model cites; the output guard turns it back into the real chunk id."""
    parts: list[str] = []
    labels: dict[str, str] = {}
    for number, chunk in enumerate(chunks, start=1):
        label = f"S{number}"
        labels[label] = chunk.chunk_id
        pages = ""
        if chunk.page_start is not None:
            pages = f" pages={chunk.page_start}-{chunk.page_end or chunk.page_start}"
        header = (
            f"<<<DOCUMENT id={label} source={_label(chunk.source)} "
            f"effective={_label(chunk.effective_date)} clause={_label(chunk.clause_ref or '-')}"
            f"{pages}>>>"
        )
        parts.append(f"{header}\n{neutralise(chunk.text)}\n<<<END DOCUMENT>>>")
    return "\n\n".join(parts), labels
