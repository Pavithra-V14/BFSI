"""List every place where SQL text is built from parts (what bandit reports as B608).

    uv run python scripts/sql_sites.py            # print the sites
    uv run python scripts/sql_sites.py --check    # exit 1 when a site is not in the reviewed list

A "site" is a string built by an f-string, a `+`, a `%` or `.format` that holds SQL (a SELECT ...
FROM, DELETE FROM, INSERT INTO ... VALUES or UPDATE ... SET statement, the same shapes bandit's B608
looks for). For each one this prints the file, the line, the source line and the expressions that
are put into the text. The reviewed list is `docs/sql_sites.json`: the owner's accepted-findings
baseline accepts B608 as a class only because every site in that list puts nothing but a table or
column name or a run of `?` placeholders into the text, and every value goes in as a bound
parameter. A NEW site is therefore not accepted until someone reads it and adds it to the list
(`tests/test_sql_sites.py` fails until then).

Sites are matched by file and source line TEXT, not by line number, so an edit above a site does
not turn it into a new one. Standard library only; no database is touched.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("app", "scripts", "load", "frontend")
LIST_PATH = ROOT / "docs" / "sql_sites.json"
# the shapes bandit's B608 looks for
SQL = re.compile(
    r"(select\s.*\sfrom\s|delete\s+from\s|insert\s+into\s.*\svalues\s|update\s.*\sset\s)",
    re.IGNORECASE | re.DOTALL,
)


def _constants(node: ast.AST) -> str:
    return "".join(
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )


def _interpolated(node: ast.AST) -> list[str]:
    """The source text of every non-constant part that goes into the string."""
    parts: list[str] = []
    if isinstance(node, ast.JoinedStr):
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                parts.append(ast.unparse(value.value))
    elif isinstance(node, ast.BinOp):
        for side in (node.left, node.right):
            if isinstance(side, ast.Constant):
                continue
            if isinstance(side, ast.BinOp | ast.JoinedStr):
                parts.extend(_interpolated(side))
            else:
                parts.append(ast.unparse(side))
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        parts.extend(ast.unparse(a) for a in node.args)
        parts.extend(ast.unparse(k.value) for k in node.keywords)
    return parts


def sites_in(path: Path, root: Path = ROOT) -> list[dict]:
    text = path.read_text("utf-8")
    lines = text.split("\n")
    tree = ast.parse(text)
    found: dict[int, dict] = {}
    inner: set[int] = set()
    for node in ast.walk(tree):
        built = isinstance(node, ast.JoinedStr) or (
            isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod)
        )
        formatted = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
            and isinstance(node.func.value, ast.Constant)
        )
        if not (built or formatted):
            continue
        # a nested part of a bigger site is one site, reported once at the outermost node
        for child in ast.walk(node):
            if child is not node and isinstance(child, ast.JoinedStr | ast.BinOp):
                inner.add(id(child))
        if id(node) in inner:
            continue
        parts = _interpolated(node)
        if not parts or not SQL.search(_constants(node)):
            continue
        number = getattr(node, "lineno", 0)
        found[number] = {
            "file": path.relative_to(root).as_posix(),
            "line": number,
            "source": lines[number - 1].strip(),
            "interpolated": parts,
        }
    return [found[n] for n in sorted(found)]


def all_sites(root: Path = ROOT, folders: Iterable[str] = SCANNED) -> list[dict]:
    sites: list[dict] = []
    for folder in folders:
        for path in sorted((root / folder).rglob("*.py")):
            sites.extend(sites_in(path, root))
    return sites


def key(site: dict) -> tuple[str, str, tuple[str, ...]]:
    return (site["file"], site["source"], tuple(site["interpolated"]))


def new_sites(sites: list[dict], reviewed: list[dict]) -> list[dict]:
    """Sites that the reviewed list does not account for (by file, source text and the
    interpolated expressions, counted: two identical sites need two entries)."""
    available: dict[tuple, int] = {}
    for entry in reviewed:
        available[key(entry)] = available.get(key(entry), 0) + 1
    fresh = []
    for site in sites:
        k = key(site)
        if available.get(k, 0) > 0:
            available[k] -= 1
        else:
            fresh.append(site)
    return fresh


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    sites = all_sites()
    if "--check" in args:
        try:
            reviewed = json.loads(LIST_PATH.read_text("utf-8"))["sites"]
        except (OSError, ValueError, KeyError):
            out("error: docs/sql_sites.json is missing or unreadable")
            return 2
        fresh = new_sites(sites, reviewed)
        out(f"{len(sites)} site(s) found, {len(reviewed)} reviewed, {len(fresh)} new")
        for site in fresh:
            out(f"NEW {site['file']}:{site['line']}: {site['source']}  <- {site['interpolated']}")
        return 1 if fresh else 0
    for site in sites:
        out(f"{site['file']}:{site['line']}: {site['source']}")
        out(f"    interpolated: {', '.join(site['interpolated'])}")
    out(f"{len(sites)} site(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
