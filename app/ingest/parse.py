"""Parsing (spec 6.1): one page at a time, so one bad page cannot sink the document.

`parse_pdf` asks a page parser for each page. A page that raises is recorded as a failed page
(by exception class name only, never page text) and the rest of the document is still parsed.
The caller must not activate a document that has a failed page (6.3).

The real page parser lives in `unstructured_backend.py` and imports Unstructured only when it is
used, so the tests (and anything that merely imports this module) need no ingestion packages.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from app.ingest.model import Element, FailedPage, ParsedDocument

PageParser = Callable[[Path, int], list[Element]]

FURNITURE_SHARE = 0.6  # text repeated on this share of the pages (and at least 2) is furniture
_DIGITS = re.compile(r"\d+")
_PAGE_MARKER = re.compile(r"(?:^|[|–—-])\s*page\s*\d+(?:\s*(?:of|/)\s*\d+)?\s*$", re.IGNORECASE)


def _furniture_key(text: str) -> str:
    """The text, lower-cased with spaces collapsed. A line that ENDS with a page marker ("... |
    Page 2 of 3") has its numbers blanked, so it matches on every page; any other line must match
    exactly ("Part 2. Scope" is not the same line as "Part 3. Scope", and a sentence that merely
    mentions a page is never blanked)."""
    key = " ".join(text.lower().split())
    return _DIGITS.sub("#", key) if _PAGE_MARKER.search(key) else key


def drop_page_furniture(elements: list[Element], parsed_pages: int) -> tuple[list[Element], int]:
    """Remove running headers and footers: a title, paragraph or list item whose text (numbers
    blanked) appears on at least 60% of the parsed pages and on at least two of them. Tables and
    images are never dropped. Returns the kept elements and how many were dropped.

    Left in, a footer lands at the end of whatever clause is last on the page and a repeated
    header title resets the heading in the middle of a clause that runs over a page."""
    if parsed_pages < 2:
        return elements, 0
    pages_of: dict[str, set[int]] = {}
    for element in elements:
        if element.kind in ("title", "text", "list_item"):
            pages_of.setdefault(_furniture_key(element.text), set()).add(element.page)
    needed = max(2, math.ceil(FURNITURE_SHARE * parsed_pages))
    furniture = {key for key, pages in pages_of.items() if len(pages) >= needed}
    kept = [
        e
        for e in elements
        if not (e.kind in ("title", "text", "list_item") and _furniture_key(e.text) in furniture)
    ]
    return kept, len(elements) - len(kept)


def parse_pdf(
    path: Path,
    *,
    page_count: int,
    parse_page: PageParser,
    parser: str = "unstructured",
) -> ParsedDocument:
    elements: list[Element] = []
    failed: list[FailedPage] = []
    for number in range(1, page_count + 1):
        try:
            found = parse_page(path, number)
        except Exception as error:  # any parser failure is a failed page, not a crash
            failed.append(FailedPage(number, type(error).__name__))
            continue
        for element in found:
            text = element.text.strip()
            if not text and not element.rows and element.image is None:
                continue
            elements.append(replace(element, text=text, page=number))
    elements, dropped = drop_page_furniture(elements, page_count - len(failed))
    return ParsedDocument(
        elements=elements,
        page_count=page_count,
        failed_pages=failed,
        parser=parser,
        furniture_dropped=dropped,
    )
