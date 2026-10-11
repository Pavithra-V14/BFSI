"""The sample corpus manifest (spec 20) and the rule that keeps real customer data out of the
evaluation (owner addition c): a case may name only documents that are in the manifest.

The manifest lists the three illustrative documents with their SHA-256, the document id they are
ingested under, and the access role they get. `verify_files` checks that the files on disk are the
listed ones (so a swapped or edited file is noticed before it is ingested or evaluated).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
MANIFEST_PATH = DATA / "corpus_manifest.json"
ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class CorpusDoc:
    doc_id: str
    file: str
    sha256: str
    doc_type: str
    access_role: str
    product_line: str
    title: str


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, CorpusDoc]:
    data = json.loads(path.read_text("utf-8"))
    docs: dict[str, CorpusDoc] = {}
    for item in data["documents"]:
        doc = CorpusDoc(
            doc_id=str(item["doc_id"]),
            file=str(item["file"]),
            sha256=str(item["sha256"]).lower(),
            doc_type=str(item["doc_type"]),
            access_role=str(item["access_role"]),
            product_line=str(item["product_line"]),
            title=str(item["title"]),
        )
        if doc.doc_id in docs:
            raise ValueError("the corpus manifest lists a document id twice")
        docs[doc.doc_id] = doc
    return docs


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_files(manifest: dict[str, CorpusDoc] | None = None, root: Path = ROOT) -> list[str]:
    """The ids of listed documents whose file is missing or is not the listed file."""
    manifest = manifest or load_manifest()
    bad: list[str] = []
    for doc in manifest.values():
        path = root / doc.file
        if not path.is_file() or sha256_of(path) != doc.sha256:
            bad.append(doc.doc_id)
    return bad


def unknown_sources(doc_ids: list[str], manifest: dict[str, CorpusDoc] | None = None) -> list[str]:
    """The document ids that are not in the manifest (a case that names one is refused)."""
    manifest = manifest or load_manifest()
    return sorted({d for d in doc_ids if d not in manifest})
