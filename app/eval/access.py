"""How the running API reaches the evaluation databases (light; no heavy import).

Two environments, never mixed (spec 15.5):
- "local" (test): a SQLite file on the admin machine. Present only where the API runs on that
  machine and the file exists;
- "production": the separate cloud evaluation database, `EVAL_DATABASE_URL`. On Render this is the
  only one; the sampler writes to it.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.db.connection import Database, connect
from app.eval import store
from app.eval.sampling import Sampler

Starter = Callable[[list[str]], Any]


def _spawn(args: list[str]) -> Any:
    """Start the command-line runner as its own process (the admin machine's shell settings, so
    the evaluation keys stay out of this process). Detached; nothing is read from it."""
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "app.eval", *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@dataclass
class EvalAccess:
    local_path: Path | None = None
    production_url: str | None = None
    sampler: Sampler | None = None
    starter: Starter = field(default=_spawn)
    env: Mapping[str, str] | None = None  # the shell settings of this process (read, never shown)

    def environments(self) -> list[str]:
        out = []
        if self.local_path is not None and self.local_path.is_file():
            out.append("local")
        if self.production_url:
            out.append("production")
        return out

    def database(self, environment: str) -> Database | None:
        if environment == "local" and self.local_path is not None and self.local_path.is_file():
            return connect(store.sqlite_url(self.local_path))
        if environment == "production" and self.production_url:
            return connect(self.production_url)
        return None
