"""A tiny .env loader for scripts the owner runs by hand (standard library only).

Rules, all enforced by tests:
- It loads only the names it is told to allow (the scripts pass the names listed in
  .env.example that the script needs).
- It never prints a value, never returns a value and never writes a value anywhere:
  values go into the given mapping (in memory) and nowhere else.
- A variable already set in the shell wins over the file.
- Empty values and malformed lines are ignored.
Claude never runs a script that uses this loader and never reads the real file.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, MutableMapping
from pathlib import Path

_EXAMPLE_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=")
_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def example_names(example_path: Path) -> set[str]:
    """Variable names listed in an .env.example file (names only)."""
    names: set[str] = set()
    for line in example_path.read_text(encoding="utf-8-sig").splitlines():
        match = _EXAMPLE_LINE.match(line)
        if match:
            names.add(match.group(1))
    return names


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def load_env_file(
    path: Path, allowed: Iterable[str], environ: MutableMapping[str, str]
) -> list[str]:
    """Copy allowed, non-empty variables from the file into `environ`.

    Returns the sorted NAMES that were loaded. Existing non-empty variables in `environ`
    are left alone. Unquoted values are taken as written (no inline comment handling).
    """
    allowed_names = set(allowed)
    loaded: list[str] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not _NAME.match(name) or name not in allowed_names:
            continue
        value = _unquote(value.strip())
        if not value or environ.get(name):
            continue
        environ[name] = value
        loaded.append(name)
    return sorted(set(loaded))
