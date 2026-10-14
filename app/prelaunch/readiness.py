"""The production-readiness score (spec 17.6: at least 70 of 100, no category below 12 of 20).

`docs/READINESS.md` holds five categories of 20 points, each a table of lines with a maximum, a
score and the evidence for it. The rules are code, and they are conservative on purpose:

- a line with NO valid citation scores 0, whatever score is written (evidence.py);
- a score above the line's maximum is cut to the maximum;
- the maxima of a category must add up to exactly 20, and there must be exactly five categories,
  or the page is "malformed" and the total is not computed (a gate must not pass on a page that
  has lost a line);
- the verdict is alert only (build rule 5): it is a word on a report, never a block.

The page also says it is re-scored by a second review; `second_review` reads the one line that
records it, so that a first-pass score is never mistaken for a reviewed one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.prelaunch.evidence import ROOT, has_evidence

READINESS_PATH = ROOT / "docs" / "READINESS.md"
CATEGORY_COUNT = 5
CATEGORY_MAX = 20
PASS_TOTAL = 70
PASS_CATEGORY = 12

_HEADING = re.compile(r"^##\s+Category:\s+(.+?)\s*$")
_ID = re.compile(r"^[A-Z]{2}\d{1,2}$")
_SECOND = re.compile(r"^\s*-\s*\*\*Second review:\*\*\s*(.*)$", re.M)


@dataclass
class Line:
    id: str
    text: str
    maximum: int
    claimed: float
    evidence: str
    points: float = 0.0  # what counts: 0 without a valid citation
    note: str = ""


@dataclass
class Category:
    name: str
    lines: list[Line] = field(default_factory=list)

    @property
    def maximum(self) -> int:
        return sum(line.maximum for line in self.lines)

    @property
    def score(self) -> float:
        return round(sum(line.points for line in self.lines), 2)


@dataclass
class Readiness:
    categories: list[Category]
    problems: list[str]

    @property
    def total(self) -> float:
        return round(sum(c.score for c in self.categories), 2)

    @property
    def well_formed(self) -> bool:
        return not self.problems

    @property
    def passes(self) -> bool:
        return (
            self.well_formed
            and self.total >= PASS_TOTAL
            and all(c.score >= PASS_CATEGORY for c in self.categories)
        )


def _number(cell: str) -> float | None:
    try:
        value = float(cell.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def parse(text: str, root: Path = ROOT) -> Readiness:
    categories: list[Category] = []
    problems: list[str] = []
    current: Category | None = None
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        heading = _HEADING.match(line)
        if heading:
            current = Category(heading.group(1))
            categories.append(current)
            continue
        if current is None or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or not _ID.match(cells[0]):
            continue
        maximum = _number(cells[2])
        claimed = _number(cells[3])
        if maximum is None or maximum != int(maximum) or maximum <= 0:
            problems.append(f"{cells[0]}: the maximum is not a whole number above 0")
            continue
        if claimed is None and cells[3] not in ("", "-"):
            problems.append(f"{cells[0]}: the score is not a number")
        entry = Line(cells[0], cells[1], int(maximum), claimed or 0.0, cells[4])
        if entry.claimed > 0 and not has_evidence(entry.evidence, root):
            entry.note = "no valid citation: scores 0"
        elif entry.claimed > 0:
            entry.points = min(entry.claimed, float(entry.maximum))
            if entry.claimed > entry.maximum:
                entry.note = "cut to the maximum"
        current.lines.append(entry)
    if len(categories) != CATEGORY_COUNT:
        problems.append(f"{len(categories)} categories, not {CATEGORY_COUNT}")
    for category in categories:
        if category.maximum != CATEGORY_MAX:
            problems.append(f"{category.name}: the maxima add up to {category.maximum}, not 20")
        ids = [line.id for line in category.lines]
        if len(set(ids)) != len(ids):
            problems.append(f"{category.name}: a line id is used twice")
    return Readiness(categories, problems)


def load(path: Path = READINESS_PATH, root: Path = ROOT) -> Readiness:
    return parse(path.read_text("utf-8"), root)


def second_review(text: str) -> str | None:
    """The text of the 'Second review' line, None when it is empty or still a placeholder."""
    found = _SECOND.search(text)
    if not found:
        return None
    value = found.group(1).strip()
    if not value or value.lower().startswith(("not done", "to be", "none", "-")):
        return None
    return value
