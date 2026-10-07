"""Table cells from the PDF's own text and ruling lines (spec 6.1: "tables are kept as tables").

Unstructured finds a table and reads its cells by OCR. On a digital PDF that OCR garbles cells
(light text on a dark header becomes "Seely GG teilig) allicle"), while the PDF's text layer is
clean. This module rebuilds the cells from the text layer: the horizontal and vertical ruling lines
inside the table's box give the rows and columns, and each word goes to the cell its centre is in.

It returns None when the table has no usable grid (no ruling lines, or words that fall outside
every cell), and the caller then keeps the OCR structure. Pure functions over `PageLayout`; the
real layout comes from pdfminer in `read_layout`, imported only when used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

CLUSTER_TOLERANCE = 1.5  # points: lines this close are one line
MIN_COVERAGE = 0.98  # share of the table's words that must land in a cell
MIN_RULE_SHARE = 0.5  # a ruling line must span half the table to count


@dataclass(frozen=True)
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class PageLayout:
    """One page in PDF coordinates (origin bottom-left)."""

    x0: float
    y0: float
    x1: float
    y1: float
    words: list[Word] = field(default_factory=list)
    h_rules: list[tuple[float, float, float]] = field(default_factory=list)  # (y, x_from, x_to)
    v_rules: list[tuple[float, float, float]] = field(default_factory=list)  # (x, y_from, y_to)

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


Box = tuple[float, float, float, float]  # x0, y0, x1, y1 in PDF coordinates


def _lines(
    rules: list[tuple[float, float, float]],
    across: tuple[float, float],
    along: tuple[float, float],
    pad: float,
) -> list[float]:
    """The positions of the ruling lines of one direction inside a table.

    Rules at (almost) the same position are one line, and their lengths add up: a border drawn
    row by row in short pieces is still a border when the pieces together cover half the table."""
    lo, hi = across
    start, end = along
    pieces = sorted(
        (c, max(a, start - pad), min(b, end + pad))
        for c, a, b in rules
        if lo - pad <= c <= hi + pad and min(b, end + pad) > max(a, start - pad)
    )
    groups: list[list[tuple[float, float, float]]] = []
    for piece in pieces:
        if groups and piece[0] - groups[-1][-1][0] <= CLUSTER_TOLERANCE:
            groups[-1].append(piece)
        else:
            groups.append([piece])
    lines = []
    for group in groups:
        covered = sum(b - a for _, a, b in group)
        if covered >= MIN_RULE_SHARE * (end - start):
            lines.append(sum(c for c, _, _ in group) / len(group))
    return lines


def _extend(
    lines: list[float], lows: list[float], highs: list[float], centres: list[float]
) -> list[float]:
    """Add an outer line below / above the lines when words lie beyond them."""
    extended = list(lines)
    below = [low for low, centre in zip(lows, centres, strict=True) if centre < lines[0]]
    above = [high for high, centre in zip(highs, centres, strict=True) if centre >= lines[-1]]
    if below:
        extended.insert(0, min(below) - 1)
    if above:
        extended.append(max(above) + 1)
    return extended


def _band(value: float, lines: list[float]) -> int | None:
    for index in range(len(lines) - 1):
        if lines[index] <= value < lines[index + 1]:
            return index
    return None


def box_from_pixels(
    layout: PageLayout, points: list[tuple[float, float]], width: float, height: float
) -> Box:
    """A table's box in PDF coordinates, from Unstructured's pixel points and the pixel size of
    the page it rendered (the page's top edge is y = 0 in pixels, y1 in the PDF)."""
    sx, sy = layout.width / width, layout.height / height
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (
        layout.x0 + min(xs) * sx,
        layout.y1 - max(ys) * sy,
        layout.x0 + max(xs) * sx,
        layout.y1 - min(ys) * sy,
    )


def grid_rows(layout: PageLayout, box: Box, pad: float = 3.0) -> tuple[tuple[str, ...], ...] | None:
    x0, y0, x1, y1 = box
    inside = [
        w for w in layout.words if x0 - pad <= w.cx <= x1 + pad and y0 - pad <= w.cy <= y1 + pad
    ]
    if not inside:
        return None
    ys = _lines(layout.h_rules, (y0, y1), (x0, x1), pad)
    xs = _lines(layout.v_rules, (x0, x1), (y0, y1), pad)
    if len(ys) < 2 or len(xs) < 2:
        return None  # no ruling lines at all: not a ruled table
    # A border that is not drawn (the right edge of a two-column table, the line above a title
    # row) is the outer edge of the words beyond the last drawn line.
    ys = _extend(ys, [w.y0 for w in inside], [w.y1 for w in inside], [w.cy for w in inside])
    xs = _extend(xs, [w.x0 for w in inside], [w.x1 for w in inside], [w.cx for w in inside])
    if len(ys) < 3 or len(xs) < 3:
        return None  # fewer than two rows or two columns: no grid to read
    cells: dict[tuple[int, int], list[Word]] = {}
    placed = 0
    for word in inside:
        row, col = _band(word.cy, ys), _band(word.cx, xs)
        if row is None or col is None:
            continue
        cells.setdefault((row, col), []).append(word)
        placed += 1
    if placed / len(inside) < MIN_COVERAGE:
        return None
    row_count, col_count = len(ys) - 1, len(xs) - 1
    table: list[list[str]] = []
    for row in range(row_count - 1, -1, -1):  # top row first (PDF y grows upwards)
        line = []
        for col in range(col_count):
            words = sorted(cells.get((row, col), []), key=lambda w: (-round(w.cy / 2), w.x0))
            line.append(" ".join(w.text for w in words))
        table.append(line)
    kept_cols = [c for c in range(col_count) if any(row[c] for row in table)]
    rows = tuple(tuple(row[c] for c in kept_cols) for row in table if any(row))
    if len(rows) < 2 or len(kept_cols) < 2:
        return None
    return rows


def read_layout(path: Path) -> PageLayout:
    """The first page of a PDF as words and ruling lines (pdfminer, installed with
    unstructured[pdf])."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTAnno, LTChar, LTCurve, LTLine, LTRect, LTTextLine

    page = next(iter(extract_pages(str(path))))
    layout = PageLayout(*page.bbox)
    thin = 1.5

    def add_words(line) -> None:
        chars: list[LTChar] = []

        def flush() -> None:
            if chars:
                layout.words.append(
                    Word(
                        min(c.x0 for c in chars),
                        min(c.y0 for c in chars),
                        max(c.x1 for c in chars),
                        max(c.y1 for c in chars),
                        "".join(c.get_text() for c in chars),
                    )
                )
                chars.clear()

        for item in line:
            if isinstance(item, LTChar) and item.get_text().strip():
                if chars and item.x0 - chars[-1].x1 > 0.4 * max(item.size, 1):
                    flush()
                chars.append(item)
            elif isinstance(item, (LTAnno, LTChar)):
                flush()
        flush()

    def walk(container) -> None:
        for item in container:
            if isinstance(item, LTTextLine):
                add_words(item)
            elif isinstance(item, (LTLine, LTRect, LTCurve)):
                width, height = item.x1 - item.x0, item.y1 - item.y0
                if height < thin <= width:
                    layout.h_rules.append(((item.y0 + item.y1) / 2, item.x0, item.x1))
                elif width < thin <= height:
                    layout.v_rules.append(((item.x0 + item.x1) / 2, item.y0, item.y1))
                elif isinstance(item, LTRect) and width >= thin and height >= thin:
                    # A filled cell or band: its edges are cell edges too.
                    layout.h_rules += [(item.y0, item.x0, item.x1), (item.y1, item.x0, item.x1)]
                    layout.v_rules += [(item.x0, item.y0, item.y1), (item.x1, item.y0, item.y1)]
            elif hasattr(item, "__iter__"):
                walk(item)

    walk(page)
    return layout
