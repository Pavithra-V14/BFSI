"""Enrichment of new and changed chunks, before they are embedded and written (spec 6.1, 6.2, 6.8,
9.3). It plugs into `ingest_document` as the `enrich` callable.

For each chunk it, in order:
1. captions an image chunk (Gemini, through the gateway), prefixed with page and heading context;
2. writes a table chunk's one-line summary (decide tier);
3. labels the chunk with one of the eight clause categories (decide tier);
4. scans the text for instruction-like content and, on a match, quarantines the chunk (stored
   inactive until the owner approves it);
5. extracts the document's allow-list entities and records them (6.8).

A caption or summary that fails does NOT stop the run half way: the page is reported as failed, the
document is not activated, and nothing is written (6.3). A decide-tier label that cannot be had is
"general". For a customer-specific document no model is called at all: nothing leaves the machine,
no image is captioned, labels stay "general", and every entity candidate is rejected.

Results of the paid calls are kept in a local cache (cache.py), so a run repeated after a failed
page does not ask again for the pages that worked.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.ingest.cache import DiskCache
from app.ingest.entities import (
    DEFAULT_COMPANY_NAMES,
    Classify,
    Ner,
    extract_candidates,
    record_candidates,
)
from app.ingest.index import Enricher
from app.ingest.model import Chunk, DocRequest, FailedPage
from app.ingest.scan import scan_text

CAPTION_MAX_WORDS = 200  # about 250 tokens (spec 6.2: image chunks are 100-250 tokens)


@dataclass
class EnrichTools:
    caption: Callable[[bytes], str]
    summarise: Callable[[str], str]
    label: Callable[[str], str]
    classify: Callable[[str, str], float | None]
    ner: Ner
    db: Any  # app.db.connection.Database
    cache: DiskCache = field(default_factory=lambda: DiskCache(None))
    company_names: tuple[str, ...] = DEFAULT_COMPANY_NAMES
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


def _caption_text(chunk: Chunk, caption: str) -> str:
    context = " > ".join(chunk.heading_path)
    prefix = f"Page {chunk.page_start}" + (f", {context}" if context else "")
    return f"{prefix}\n{' '.join(caption.split()[:CAPTION_MAX_WORDS])}"


def make_enricher(tools: EnrichTools) -> Enricher:
    def cached(kind: str, key: str, produce: Callable[[], str]) -> str:
        hit = tools.cache.get(kind, key)
        if hit is not None:
            return hit
        value = produce()
        if value:
            tools.cache.put(kind, key, value)
        return value

    def enrich(chunks: list[Chunk], request: DocRequest) -> list[FailedPage]:
        local_only = request.doc_type == "customer-specific"
        failed: dict[int, str] = {}

        def fail(chunk: Chunk, error: Exception) -> None:
            failed.setdefault(chunk.page_start, type(error).__name__)

        for chunk in chunks:
            if local_only:
                continue
            if chunk.kind == "image_caption" and chunk.image_data is not None:
                try:
                    caption = cached(
                        "caption", chunk.source, lambda c=chunk: tools.caption(c.image_data)
                    )
                    if not caption.strip():
                        raise ValueError("EmptyCaption")
                    chunk.text = _caption_text(chunk, caption)
                except Exception as error:
                    fail(chunk, error)
            elif chunk.kind == "table":
                try:
                    chunk.summary = (
                        cached("summary", chunk.source, lambda c=chunk: tools.summarise(c.text))
                        .splitlines()[0]
                        .strip()
                    )
                except Exception as error:
                    fail(chunk, error)
        for chunk in chunks:
            if local_only or not chunk.text.strip():
                continue
            try:
                chunk.clause_type = (
                    cached("label", chunk.text, lambda c=chunk: tools.label(c.text)) or "general"
                )
            except Exception:  # a label is a nicety: "general" is always acceptable
                chunk.clause_type = "general"
        for chunk in chunks:
            hits = scan_text(chunk.text) + (scan_text(chunk.summary) if chunk.summary else [])
            if hits:
                chunk.quarantine = "pending"
                chunk.quarantine_reason = ",".join(sorted(set(hits)))
        if failed:  # the document will not be activated: leave the main database untouched
            return [FailedPage(page, reason) for page, reason in sorted(failed.items())]

        def cached_classify(term: str, context: str) -> float | None:
            key = f"{term}\x00{context}"
            hit = tools.cache.get("entity", key)
            if hit is not None:
                return float(hit)
            value = tools.classify(term, context)
            if value is not None:
                tools.cache.put("entity", key, repr(value))
            return value

        classify: Classify | None = None if local_only else cached_classify

        texts: Iterable[tuple[str, tuple[str, ...]]] = [
            (c.text, c.heading_path) for c in chunks if c.text.strip()
        ]
        candidates = extract_candidates(texts, request, tools.ner, tools.company_names)
        with tools.db.transaction(write=True):
            record_candidates(tools.db, request, candidates, classify, tools.now())
        return []

    return enrich
