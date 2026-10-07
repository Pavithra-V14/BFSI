"""Where ingestion keeps its local files. Every folder is anchored to the PROJECT ROOT, not to the
folder the command happens to be started from, so two terminals always share one set of locks and
markers (a lock folder that moved with the working directory would not exclude anything).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ORIGINALS_DIR = ROOT / "originals"  # your original PDFs: the only copy, back it up (git-ignored)
LOCKS_DIR = ORIGINALS_DIR / ".locks"  # one lock file per document
PENDING_DIR = ORIGINALS_DIR / ".pending"  # one pending-finish marker per document
CACHE_DIR = ORIGINALS_DIR / ".cache"  # saved model answers, may be deleted at any time
ENV_FILE = ROOT / ".env"  # read only when you pass --load-env
ENV_EXAMPLE = ROOT / ".env.example"
