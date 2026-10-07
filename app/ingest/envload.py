"""The opt-in `--load-env` for the ingest commands (off unless you pass the flag).

It is the same restricted loader the gateway smoke test uses (scripts/envfile.py):
- only the names in INGEST_VARIABLES and the gateway's own variable list, and only those that
  `.env.example` lists, are ever copied from the file;
- a variable already set in your shell wins over the file;
- it prints the NAMES it loaded and never a value;
- it refuses to run on Render.

Without `--load-env` nothing here runs and no file is opened: settings come from the shell.
"""

from __future__ import annotations

import sys
from collections.abc import MutableMapping
from pathlib import Path

from app.gateway.config import ENV_NAMES
from app.gateway.embeddings import running_on_render
from app.ingest import paths
from app.ingest.model import IngestError

INGEST_VARIABLES = (
    "DATABASE_URL",
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "UPSTASH_REDIS_URL",
    "CACHE_TTL_SECONDS",
    "COMPANY_NAMES",
    "COMPANY_DOMAINS",
)


def load_env_for_ingest(
    environ: MutableMapping[str, str],
    env_path: Path | None = None,
    example_path: Path | None = None,
) -> str:
    """Load the named variables from the env file into `environ`; return a message of NAMES."""
    if running_on_render(environ):
        raise IngestError(
            "--load-env is refused on Render: ingestion runs only on the admin machine"
        )
    if str(paths.ROOT) not in sys.path:
        sys.path.insert(0, str(paths.ROOT))  # so `scripts.envfile` is found from any folder
    from scripts.envfile import example_names, load_env_file

    path = env_path or paths.ENV_FILE
    if not path.is_file():
        return "--load-env: no env file found, using the variables already set in this shell"
    allowed = example_names(example_path or paths.ENV_EXAMPLE) & (
        set(INGEST_VARIABLES) | set(ENV_NAMES)
    )
    names = load_env_file(path, allowed, environ)
    listed = ", ".join(names) or "none"
    return f"--load-env: loaded {len(names)} variable(s): {listed} (values are never shown)"
