"""Chunking (spec 6.2) [P]. Pure functions: parsed elements in, chunk drafts out.

- A numbered clause ("Section 3.", "Clause 2.1", "3.2") starts a new chunk group; unnumbered
  text is grouped under the heading the parser found.
- Target about 450 tokens, hard maximum about 600. A clause is split only when it is over the
  hard maximum; the pieces break at sentence boundaries, repeat the clause heading, and overlap
  by about 60 tokens (overlap exists only inside a split clause).
- A table is one chunk if it fits, otherwise it is split by rows with the header row repeated.
- An image becomes an `ImageItem`; the caption chunk is made later (captions.py).
- A fragment under about 50 tokens with no clause number of its own merges into a neighbour
  under the same parent heading, and only into another unnumbered chunk. Numbered clauses are
  never merged and never absorb anything, so adding or removing a clause cannot change another
  clause's chunk.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from app.ingest.model import Chunk, Element, ImageItem, ParsedDocument

TARGET_TOKENS = 450
HARD_MAX_TOKENS = 600
OVERLAP_TOKENS = 60
MIN_FRAGMENT_TOKENS = 50

TokenCount = Callable[[str], int]

_NUMBER = re.compile(
    r"^\s*(?:(section|clause|article)\s+)?(\d{1,3}[A-Za-z]?(?:\.\d{1,3}[A-Za-z]?)*)(\.|\))?(?=\s|$)",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])")


def clause_number(text: str, kind: str) -> str | None:
    """The clause number a title or paragraph starts with, or None.

    A bare number counts only for a title; in running text it needs a "Section/Clause" word, a
    closing "." or ")", or a dotted form such as 3.2 (so "100 crore ..." is not a clause).
    """
    found = _NUMBER.match(text)
    if not found:
        return None
    prefix, number, closer = found.groups()
    if prefix or closer or "." in number or kind == "title":
        return number
    return None


_LABEL_TITLE = re.compile(
    r"^\s*(?:(?:section|clause|article)\s+)?\S+?\s+([^.\n]{3,90}?)\.\s", re.IGNORECASE
)


def clause_label(text: str, ref: str) -> str:
    """The heading of a clause that starts inside a paragraph: "5. Penal Charges and Prepayment.
    Non-compliance ..." is labelled "5. Penal Charges and Prepayment"; with no short title in
    front of the first full stop (at most 10 words) it is just the clause number."""
    found = _LABEL_TITLE.match(text)
    if found and len(found.group(1).split()) <= 10:
        return f"{ref}. {found.group(1).strip()}"
    return ref


@dataclass
class _Block:
    clause_ref: str | None
    heading_path: tuple[str, ...]
    lead: str | None  # the heading line, repeated in every piece after the first
    show_lead: bool  # put the heading line at the top of the first piece too
    paragraphs: list[str] = field(default_factory=list)
    tables: list[Element] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)

    @property
    def parent(self) -> tuple[str, ...]:
        return self.heading_path[:-1]


@dataclass
class ChunkedDocument:
    chunks: list[Chunk]
    images: list[ImageItem]


def _units(paragraphs: list[str]) -> list[tuple[str, str]]:
    """(separator, sentence) pairs: a paragraph break is "\\n", a sentence break is " "."""
    units: list[tuple[str, str]] = []
    for paragraph in paragraphs:
        sentences = [s.strip() for s in _SENTENCE.split(paragraph) if s.strip()]
        for position, sentence in enumerate(sentences):
            units.append(("\n" if position == 0 else " ", sentence))
    return units


def _join(units: list[tuple[str, str]]) -> str:
    text = ""
    for position, (separator, sentence) in enumerate(units):
        text += (separator if position else "") + sentence
    return text


def _split_long(sentence: str, count: TokenCount) -> list[str]:
    """A single sentence longer than the hard maximum is cut at word boundaries."""
    words = sentence.split()
    pieces: list[str] = []
    current: list[str] = []
    for word in words:
        current.append(word)
        if count(" ".join(current)) >= TARGET_TOKENS:
            pieces.append(" ".join(current))
            current = []
    if current:
        pieces.append(" ".join(current))
    return pieces


def _pieces(block: _Block, count: TokenCount) -> list[str]:
    lead = f"{block.lead}\n" if block.lead else ""
    first_prefix = lead if block.show_lead else ""
    units = _units(block.paragraphs)
    if not units:
        return []
    whole = first_prefix + _join(units)
    if count(whole) <= HARD_MAX_TOKENS:
        return [whole]
    expanded: list[tuple[str, str]] = []
    for separator, sentence in units:
        if count(sentence) > HARD_MAX_TOKENS:
            for position, part in enumerate(_split_long(sentence, count)):
                expanded.append((separator if position == 0 else " ", part))
        else:
            expanded.append((separator, sentence))
    pieces: list[str] = []
    current: list[tuple[str, str]] = []
    prefix = first_prefix
    tokens = count(prefix)
    for unit in expanded:
        cost = count(unit[1])
        if current and tokens + cost > TARGET_TOKENS:
            pieces.append(prefix + _join(current))
            overlap: list[tuple[str, str]] = []
            used = 0
            for previous in reversed(current[1:]):  # never the whole piece again
                used += count(previous[1])
                if used > OVERLAP_TOKENS:
                    break
                overlap.insert(0, previous)
            current = overlap
            prefix = lead
            tokens = count(prefix) + sum(count(s) for _, s in current)
        current.append(unit)
        tokens += cost
    if current:
        pieces.append(prefix + _join(current))
    return pieces


def _row_text(row: tuple[str, ...]) -> str:
    return " | ".join(cell for cell in row)


def _table_chunks(block: _Block, element: Element, ordinal: int, count: TokenCount) -> list[Chunk]:
    lead = f"{block.lead}\n" if block.lead else ""
    lines = [_row_text(row) for row in element.rows] or [
        line.strip() for line in element.text.splitlines() if line.strip()
    ]
    if not lines:
        return []
    groups: list[list[str]]
    if count(lead + "\n".join(lines)) <= HARD_MAX_TOKENS or len(lines) < 2:
        groups = [lines]
    else:
        header, body = lines[0], lines[1:]
        groups = []
        current: list[str] = []
        tokens = count(lead + header)
        for line in body:
            cost = count(line)
            if current and tokens + cost > TARGET_TOKENS:
                groups.append([header, *current])
                current, tokens = [], count(lead + header)
            current.append(line)
            tokens += cost
        if current:
            groups.append([header, *current])
    chunks = []
    for part, group in enumerate(groups):
        text = lead + "\n".join(group)
        chunks.append(
            Chunk(
                kind="table",
                text=text,
                source=text,
                clause_ref=block.clause_ref,
                heading_path=block.heading_path,
                page_start=element.page,
                page_end=element.page,
                part_index=part,
                ordinal=ordinal,
            )
        )
    return chunks


def _merge_fragments(blocks: list[_Block], count: TokenCount) -> list[_Block]:
    """Merge tiny unnumbered, table-free blocks into a neighbour with the same parent heading."""

    def text_of(block: _Block) -> list[str]:
        return ([block.lead] if block.lead and block.show_lead else []) + block.paragraphs

    def tiny(block: _Block) -> bool:
        return (
            block.clause_ref is None
            and not block.tables
            and bool(block.paragraphs)
            and count("\n".join(text_of(block))) < MIN_FRAGMENT_TOKENS
        )

    merged: list[_Block] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if tiny(block):
            previous = merged[-1] if merged else None
            following = blocks[index + 1] if index + 1 < len(blocks) else None
            if (
                previous
                and previous.clause_ref is None
                and previous.parent == block.parent
                and previous.paragraphs
            ):
                previous.paragraphs.extend(text_of(block))
                previous.pages.extend(block.pages)
                index += 1
                continue
            if (
                following
                and following.clause_ref is None
                and following.parent == block.parent
                and following.paragraphs
            ):
                following.paragraphs = text_of(block) + following.paragraphs
                following.pages = block.pages + following.pages
                index += 1
                continue
        merged.append(block)
        index += 1
    return merged


def chunk_document(parsed: ParsedDocument, count: TokenCount) -> ChunkedDocument:
    stack: list[tuple[int, str]] = []
    blocks: list[_Block] = []
    images: list[ImageItem] = []

    def path() -> tuple[str, ...]:
        return tuple(label[:160] for _, label in stack)

    def open_block(depth: int, label: str, ref: str | None, show_lead: bool, page: int) -> _Block:
        while stack and stack[-1][0] >= depth:
            stack.pop()
        stack.append((depth, label))
        block = _Block(ref, path(), label[:160], show_lead, pages=[page])
        blocks.append(block)
        return block

    def current(page: int) -> _Block:
        if blocks:
            return blocks[-1]
        block = _Block(None, (), None, False, pages=[page])
        blocks.append(block)
        return block

    for element in parsed.elements:
        if element.kind == "title":
            ref = clause_number(element.text, "title")
            open_block(ref.count(".") + 1 if ref else 0, element.text, ref, True, element.page)
        elif element.kind in ("text", "list_item"):
            ref = clause_number(element.text, "text") if element.kind == "text" else None
            if ref:
                block = open_block(
                    ref.count(".") + 1, clause_label(element.text, ref), ref, False, element.page
                )
            else:
                block = current(element.page)
            block.paragraphs.append(element.text)
            block.pages.append(element.page)
        elif element.kind == "table":
            block = current(element.page)
            block.tables.append(element)
            block.pages.append(element.page)
        elif element.kind == "image" and element.image:
            block = blocks[-1] if blocks else None
            images.append(
                ImageItem(
                    data=element.image,
                    page=element.page,
                    heading_path=path(),
                    clause_ref=block.clause_ref if block else None,
                )
            )

    chunks: list[Chunk] = []
    for block in _merge_fragments(blocks, count):
        pages = block.pages or [1]
        for part, text in enumerate(_pieces(block, count)):
            chunks.append(
                Chunk(
                    kind="text",
                    text=text,
                    source=text,
                    clause_ref=block.clause_ref,
                    heading_path=block.heading_path,
                    page_start=min(pages),
                    page_end=max(pages),
                    part_index=part,
                )
            )
        for ordinal, table in enumerate(block.tables):
            chunks.extend(_table_chunks(block, table, ordinal, count))
    return ChunkedDocument(chunks=chunks, images=images)
