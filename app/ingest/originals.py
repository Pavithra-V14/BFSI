"""Keeping the original PDFs (spec 11.3) [P]: a local, git-ignored `originals/` folder.

`originals/<doc_id>/<sha256>.pdf`. A file is copied once and never overwritten. This folder is
the ONLY copy the project keeps, so the owner backs it up.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from app.ingest.model import check_doc_id


def keep_original(source: Path, originals_dir: Path, doc_id: str) -> Path:
    check_doc_id(doc_id)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    target = originals_dir / doc_id / f"{digest}.pdf"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return target
