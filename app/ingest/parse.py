"""Parsing (spec 6.1): one page at a time, so one bad page cannot sink the document.

`parse_pdf` asks a page parser for each page. A page that raises, or that comes back with nothing
at all (a scanned page whose OCR found no text, a blank page), is recorded as a failed page (by
exception class name or "EmptyPage", never page text) and the rest of the document is still
parsed. The caller must not activate a document that has a failed page (6.3).

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

# The page-furniture rule [P]. A line is running furniture only when ALL of these hold:
#  - the document has at least 3 pages (the parsed ones);
#  - it is a short title, paragraph or list item (at most 25 words);
#  - it sits in an unbroken run of such lines at the very top or bottom of its page (at most 6);
#  - the same line (numbers blanked only for a trailing "Page N of M") is in that run on at least
#    60% of the parsed pages.
FURNITURE_MIN_PAGES = 3
FURNITURE_SHARE = 0.6
FURNITURE_EDGE = 6
FURNITURE_MAX_WORDS = 25
_TEXT_KINDS = ("title", "text", "list_item")
_DIGITS = re.compile(r"\d+")
_PAGE_MARKER = re.compile(r"(?:^|[|–—-])\s*page\s*\d+(?:\s*(?:of|/)\s*\d+)?\s*$", re.I)


def _furniture_key(text: str) -> str:
    """The text, lower-cased with spaces collapsed. A line that ENDS with a page marker ("... |
    Page 2 of 3") has its numbers blanked, so it matches on every page; any other line must match
    exactly ("Part 2. Scope" is not the same line as "Part 3. Scope", and a sentence that merely
    mentions a page is never blanked)."""
    key = " ".join(text.lower().split())
    return _DIGITS.sub("#", key) if _PAGE_MARKER.search(key) else key


def _eligible(element: Element) -> bool:
    return element.kind in _TEXT_KINDS and len(element.text.split()) <= FURNITURE_MAX_WORDS


def _edge_run(page: list[Element], from_top: bool) -> list[int]:
    """Indexes (into `page`) of the run of short text lines at one edge, at most FURNITURE_EDGE."""
    order = range(len(page)) if from_top else range(len(page) - 1, -1, -1)
    run: list[int] = []
    for index in order:
        if len(run) >= FURNITURE_EDGE or not _eligible(page[index]):
            break
        run.append(index)
    return run


def drop_page_furniture(
    elements: list[Element], parsed_pages: int
) -> tuple[list[Element], list[tuple[int, str]]]:
    """Remove running headers and footers (the rule above). Returns the kept elements and the
    (page, text) of every line removed, so a dry run can show exactly what went.

    Left in, a footer lands at the end of whatever clause is last on the page, and a repeated
    header title resets the heading in the middle of a clause that runs over a page."""
    if parsed_pages < FURNITURE_MIN_PAGES:
        return elements, []
    pages: dict[int, list[Element]] = {}
    for element in elements:
        pages.setdefault(element.page, []).append(element)
    candidates: dict[int, set[int]] = {}  # page -> indexes in the edge runs
    seen_on: dict[str, set[int]] = {}
    for number, page in pages.items():
        edge = set(_edge_run(page, True)) | set(_edge_run(page, False))
        candidates[number] = edge
        for index in edge:
            seen_on.setdefault(_furniture_key(page[index].text), set()).add(number)
    needed = max(2, math.ceil(FURNITURE_SHARE * parsed_pages))
    furniture = {key for key, on in seen_on.items() if len(on) >= needed}
    dropped: set[int] = set()  # id() of the elements to remove
    removed: list[tuple[int, str]] = []
    for number, page in pages.items():
        flagged = {
            index for index in candidates[number] if _furniture_key(page[index].text) in furniture
        }
        # Only the unbroken part of each edge run goes: a repeated line after a real one stays.
        for run in (_edge_run(page, True), _edge_run(page, False)):
            for index in _unbroken(run, flagged):
                if id(page[index]) not in dropped:
                    dropped.add(id(page[index]))
                    removed.append((number, page[index].text))
    kept = [element for element in elements if id(element) not in dropped]
    return kept, removed


def _unbroken(run: list[int], drop: set[int]) -> list[int]:
    """The leading part of an edge run (nearest the page edge first) that is all furniture."""
    taken = []
    for index in run:
        if index not in drop:
            break
        taken.append(index)
    return taken


def parse_pdf(
    path: Path,
    *,
    page_count: int,
    parse_page: PageParser,
    parser: str = "unstructured",
) -> ParsedDocument:
    elements: list[Element] = []
    failed: list[FailedPage] = []
    warnings: list[str] = []
    for number in range(1, page_count + 1):
        try:
            found = parse_page(path, number)
        except Exception as error:  # any parser failure is a failed page, not a crash
            failed.append(FailedPage(number, type(error).__name__))
            continue
        page_elements = []
        for element in found:
            text = element.text.strip()
            if not text and not element.rows and element.image is None:
                continue
            page_elements.append(replace(element, text=text, page=number))
        if not page_elements:  # nothing came out of the page: not "empty", failed
            failed.append(FailedPage(number, "EmptyPage"))
            continue
        for element in page_elements:
            if element.lost_words:
                warnings.append(f"page {number}: a table lost {element.lost_words} word(s)")
        elements.extend(page_elements)
    elements, removed = drop_page_furniture(elements, page_count - len(failed))
    return ParsedDocument(
        elements=elements,
        page_count=page_count,
        failed_pages=failed,
        parser=parser,
        furniture=removed,
        warnings=warnings,
    )
