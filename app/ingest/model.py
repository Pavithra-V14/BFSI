"""The data that moves through ingestion: parsed elements, chunks, and what is asked of a run."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DOC_TYPES = ("policy", "regulatory", "customer-specific")
ACCESS_ROLES = ("customer", "staff", "admin")
CONTENT_TYPES = ("text", "table", "image_caption")
# Why a document was withdrawn: a fixed list, never free text (the migration 0010 check agrees).
WITHDRAW_REASONS = ("outdated", "incorrect", "compliance", "replaced", "other")

# A stable, human-chosen slug. Never derived from a file path or a title.
_DOC_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")


class IngestError(Exception):
    """A run cannot go on. The message never carries document text."""


def check_doc_id(value: str) -> str:
    if not isinstance(value, str) or not _DOC_ID.fullmatch(value):
        raise IngestError(
            "doc_id must be a slug of 1 to 64 lowercase letters, digits and hyphens "
            "(for example 'insurance-flexi-property'); it is never taken from a file path"
        )
    return value


def check_choice(value: str, allowed: tuple[str, ...], name: str) -> str:
    if value not in allowed:
        raise IngestError(f"{name} must be one of: {', '.join(allowed)}")
    return value


CUSTOMER_REFUSAL = (
    "customer-specific documents are not accepted yet: even without captions or labels, their "
    "text would be sent to a model provider to be embedded. They stay refused until a paid or "
    "zero-retention plan is in place (Phase 16)."
)


def refuse_customer_document(doc_type: str) -> None:
    if doc_type == "customer-specific":
        raise IngestError(CUSTOMER_REFUSAL)


@dataclass(frozen=True)
class Element:
    """One typed piece of a page, as the parser reports it."""

    kind: str  # title | text | list_item | table | image
    text: str
    page: int
    rows: tuple[tuple[str, ...], ...] = ()  # a table's rows; the first row is the header
    image: bytes | None = None
    lost_words: int = 0  # words of a table that fell outside the rebuilt grid (table_grid.py)


@dataclass(frozen=True)
class FailedPage:
    page: int
    reason: str  # a short code such as an exception class name, never text from the page


@dataclass
class ParsedDocument:
    elements: list[Element]
    page_count: int
    failed_pages: list[FailedPage] = field(default_factory=list)
    parser: str = "unstructured"
    furniture: list[tuple[int, str]] = field(default_factory=list)  # (page, text) removed
    warnings: list[str] = field(default_factory=list)  # page numbers and counts only

    @property
    def furniture_dropped(self) -> int:
        return len(self.furniture)


@dataclass
class Chunk:
    """One chunk before it is stored. `source` is what the identity hash is computed from."""

    kind: str  # text | table | image_caption
    text: str  # what is stored and shown to the model
    source: str  # what is hashed: the text, or for an image "image:<sha256 of the bytes>"
    clause_ref: str | None
    heading_path: tuple[str, ...]
    page_start: int
    page_end: int
    part_index: int = 0
    ordinal: int = 0  # the nth table under its clause (tables only)
    image_ref: str | None = None
    clause_type: str = "general"
    summary: str | None = None  # a table chunk's one-line summary (not hashed)
    quarantine: str | None = None  # None | "pending" | "rejected" (9.3)
    quarantine_reason: str | None = None  # the names of the scan patterns that matched
    image_data: bytes | None = field(default=None, repr=False)  # an image chunk's picture


@dataclass
class ImageItem:
    """An image found in the document, waiting for a caption."""

    data: bytes
    page: int
    heading_path: tuple[str, ...]
    clause_ref: str | None


@dataclass
class DocRequest:
    doc_id: str
    source: str  # the original file name, for display
    title: str | None
    doc_type: str
    access_role: str
    product_line: str | None
    effective_date: str  # YYYY-MM-DD
