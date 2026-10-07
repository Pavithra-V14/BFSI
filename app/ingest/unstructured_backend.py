"""The real PDF page parser: Unstructured with poppler and tesseract (spec 6.1, admin machine only).

Imports are inside the functions, so importing this module needs no ingestion package. The
conversion helpers (`element_from_unstructured`, `rows_from_html`) take plain values and are
tested without Unstructured; the two functions that call it (`count_pages`, `parse_page`) need
`requirements-ingest.txt`, poppler and tesseract, and are exercised by the owner's own run.

Each page is cut into its own one-page PDF (pypdf, installed with unstructured[pdf]) and parsed
alone, so a page that fails to parse is reported by number and the others still parse.
"""

from __future__ import annotations

import base64
import binascii
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from app.ingest.model import Element
from app.ingest.table_grid import box_from_pixels, grid_rows, read_layout

_KINDS = {
    "Title": "title",
    "NarrativeText": "text",
    "UncategorizedText": "text",
    "Text": "text",
    "Address": "text",
    "Formula": "text",
    "ListItem": "list_item",
    "Table": "table",
    "Image": "image",
    "FigureCaption": "text",
}


class _Rows(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th") and self.rows:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self.rows:
            self.rows[-1].append(" ".join("".join(self._cell).split()))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def rows_from_html(html: str | None) -> tuple[tuple[str, ...], ...]:
    """Table rows from the HTML Unstructured gives for a table (empty rows are dropped)."""
    if not html:
        return ()
    parser = _Rows()
    parser.feed(html)
    return tuple(tuple(row) for row in parser.rows if any(cell for cell in row))


def element_from_unstructured(
    category: str,
    text: str | None,
    page: int,
    *,
    html: str | None = None,
    image_base64: str | None = None,
    grid: tuple[tuple[str, ...], ...] | None = None,
) -> Element | None:
    """One Unstructured element as ours, or None for a kind we do not use (headers, footers and
    page numbers are page furniture, not content).

    A table's `grid` (rebuilt from the PDF's own text, table_grid.py) wins over the cells read by
    OCR from the HTML, which can be garbled on a digital PDF."""
    kind = _KINDS.get(category)
    if kind is None:
        return None
    text = (text or "").strip()
    if kind == "table":
        rows = grid or rows_from_html(html)
        if not rows and not text:
            return None
        return Element("table", text, page, rows=rows)
    if kind == "image":
        try:
            data = base64.b64decode(image_base64 or "", validate=True)
        except (binascii.Error, ValueError):
            data = b""
        return Element("image", "", page, image=data) if data else None
    return Element(kind, text, page) if text else None


def count_pages(path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(path)).pages)


def parse_page(path: Path, number: int) -> list[Element]:
    from pypdf import PdfReader, PdfWriter
    from unstructured.partition.pdf import partition_pdf

    writer = PdfWriter()
    writer.add_page(PdfReader(str(path)).pages[number - 1])
    with tempfile.TemporaryDirectory() as folder:
        single = Path(folder) / "page.pdf"
        with single.open("wb") as handle:
            writer.write(handle)
        raw: list[Any] = partition_pdf(
            filename=str(single),
            strategy="hi_res",
            infer_table_structure=True,
            extract_image_block_types=["Image"],
            extract_image_block_to_payload=True,
        )
        try:
            layout = read_layout(single)
        except Exception:  # no text layer to read: the OCR cells are used as they are
            layout = None
    found: list[Element] = []
    for item in raw:
        meta = item.metadata
        grid = None
        coordinates = getattr(meta, "coordinates", None)
        if item.category == "Table" and layout is not None and coordinates is not None:
            box = box_from_pixels(
                layout,
                list(coordinates.points),
                coordinates.system.width,
                coordinates.system.height,
            )
            grid = grid_rows(layout, box)
        element = element_from_unstructured(
            item.category,
            item.text,
            number,
            html=getattr(meta, "text_as_html", None),
            image_base64=getattr(meta, "image_base64", None),
            grid=grid,
        )
        if element is not None:
            found.append(element)
    return found
