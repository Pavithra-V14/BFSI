"""Evidence for the pre-launch gate (spec 17.6, 19; Phase 16).

A claim that something passed is only as good as what it cites. This module holds the one rule
the readiness score and the gate share: an item counts only with at least one valid citation,
written in a fixed form so that a machine can check what it can check offline:

    run:<8 to 32 hex characters>      an evaluation, load or drill run id (kept by the owner)
    file:<repo path>                  a file in this repository (must exist)
    test:<repo path>::<test name>     a test (the file must exist and contain the name)
    doc:<repo path>                   a page in this repository (must exist)
    date:YYYY-MM-DD                   the day a manual check was made (a real date)

A run id cannot be looked up here (the runs live in the owner's evaluation database), so it is
accepted by its shape and the report says so. Nothing in this package calls a service, reads a
secret or writes outside the one report file the owner asks for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_RUN = re.compile(r"^run:[0-9a-f]{8,32}$")
_DATE = re.compile(r"^date:(\d{4})-(\d{2})-(\d{2})$")
_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")


@dataclass(frozen=True)
class Citation:
    text: str
    valid: bool
    why: str = ""
    checkable_offline: bool = True


def _inside_repo(rel: str, root: Path) -> Path | None:
    if not _PATH.match(rel) or ".." in rel.split("/") or rel.startswith("/"):
        return None
    path = (root / rel).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    return path


def check_citation(text: str, root: Path = ROOT) -> Citation:
    """Judge one citation token. Never raises."""
    token = text.strip()
    if _RUN.match(token):
        return Citation(token, True, "run id (shape only: the run is in your evaluation database)",
                        checkable_offline=False)  # fmt: skip
    found = _DATE.match(token)
    if found:
        try:
            date(int(found.group(1)), int(found.group(2)), int(found.group(3)))
        except ValueError:
            return Citation(token, False, "not a real date")
        return Citation(token, True, "date", checkable_offline=False)
    for kind in ("file", "doc"):
        if token.startswith(kind + ":"):
            path = _inside_repo(token[len(kind) + 1 :], root)
            if path is None or not path.is_file():
                return Citation(token, False, "the file does not exist in the repository")
            return Citation(token, True, "file exists")
    if token.startswith("test:"):
        rel, _, name = token[5:].partition("::")
        path = _inside_repo(rel, root)
        if path is None or not path.is_file() or not name:
            return Citation(token, False, "the test file or the test name is missing")
        if f"def {name}(" not in path.read_text("utf-8", errors="replace"):
            return Citation(token, False, "no such test in that file")
        return Citation(token, True, "test exists")
    return Citation(token, False, "not a run:, file:, test:, doc: or date: citation")


_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_date(text: str) -> date | None:
    """A strict ISO date `YYYY-MM-DD` (a real calendar day), or None."""
    found = _ISO.match(str(text).strip())
    if not found:
        return None
    try:
        return date(int(found.group(1)), int(found.group(2)), int(found.group(3)))
    except ValueError:
        return None


def split_citations(cell: str) -> list[str]:
    """The tokens of an evidence cell: separated by commas, semicolons or spaces."""
    return [t for t in re.split(r"[,;\s]+", cell.replace("`", "")) if t]


def valid_citations(cell: str, root: Path = ROOT) -> list[Citation]:
    return [c for c in (check_citation(t, root) for t in split_citations(cell)) if c.valid]


def has_evidence(cell: str, root: Path = ROOT) -> bool:
    return bool(valid_citations(cell, root))
