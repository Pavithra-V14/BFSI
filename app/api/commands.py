"""Running a command-line tool from the admin page (the admin machine only; never Render).

The page's "Delete" button runs `python -m app.ingest delete ...` as its own process, with this
machine's shell settings, so the stores' keys stay in that process and are never read here. The
route refuses on Render before it gets this far. What comes back is the end of the tool's output
with long tokens removed: the tool prints names and counts only (no document text, key or address).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

COMMAND_TIMEOUT_SECONDS = 150.0
PURGE_TIMEOUT_SECONDS = 300.0  # erasing a large collection page by page takes longer
MODULES = frozenset({"app.ingest", "app.maintenance"})
MAX_LINES = 8
MAX_LINE = 200
_LONG_TOKEN = re.compile(r"\S{40,}")


@dataclass
class CommandResult:
    code: int  # the tool's exit code; -1 when it did not finish in time or could not start
    lines: list[str] = field(default_factory=list)
    timed_out: bool = False


def _clean(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = _LONG_TOKEN.sub("[removed]", " ".join(raw.split()))[:MAX_LINE]
        if line:
            lines.append(line)
    return lines[-MAX_LINES:]


def run_module(
    module: str, args: list[str], timeout_s: float = COMMAND_TIMEOUT_SECONDS
) -> CommandResult:
    """Run `python -m MODULE ARGS` and wait for it. No keyboard (stdin is closed). `module` is one
    of the two command packages below, never text from a request."""
    if module not in MODULES:
        raise ValueError("not a command of this project")
    try:
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-m", module, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            env=dict(os.environ),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(-1, ["the command did not finish in time"], timed_out=True)
    except OSError:
        return CommandResult(-1, ["the command could not be started"])
    return CommandResult(done.returncode, _clean(done.stdout) + _clean(done.stderr))


def run_ingest_command(args: list[str]) -> CommandResult:
    return run_module("app.ingest", args)


def run_maintenance_command(args: list[str]) -> CommandResult:
    return run_module("app.maintenance", args, timeout_s=PURGE_TIMEOUT_SECONDS)
