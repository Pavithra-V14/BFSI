"""Chunk identity (spec 6.3): content and clause number, never position or page number.

- `content_hash` is computed on the chunk's source with all whitespace collapsed, so a
  whitespace-only edit is not a change.
- The identity key is the clause number (plus a table or image marker); a chunk with no clause
  number uses the nearest heading and its content hash instead.
- The point id is a hash of `doc_id + clause_ref + part_index`, written as a UUID (Qdrant ids
  are integers or UUIDs).
- Two chunks of one document that would get the same id (a clause number that repeats, or two
  identical unnumbered paragraphs) get `~2`, `~3` ... on the key, in document order, so every id
  is unique. That is the only place order matters, and only between exact look-alikes.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from app.ingest.model import Chunk


def content_hash(source: str) -> str:
    return hashlib.sha256(" ".join(source.split()).encode()).hexdigest()


def image_source(data: bytes) -> str:
    """What is hashed for an image caption chunk: the image itself, not the (variable) caption."""
    return "image:" + hashlib.sha256(data).hexdigest()


def point_id(doc_id: str, key: str, part_index: int) -> str:
    digest = hashlib.sha256(f"{doc_id}\x1f{key}\x1f{part_index}".encode()).digest()
    return str(uuid.UUID(bytes=digest[:16]))


@dataclass
class Identified:
    chunk: Chunk
    key: str
    chunk_id: str
    content_hash: str


def identity_key(chunk: Chunk, digest: str) -> str:
    if chunk.clause_ref is not None:
        base = chunk.clause_ref
    else:
        heading = " ".join((chunk.heading_path[-1] if chunk.heading_path else "").lower().split())
        base = f"h:{heading}:{digest[:16]}"
    if chunk.kind == "table":
        return f"{base}#t{chunk.ordinal}"
    if chunk.kind == "image_caption":
        return f"{base}#i{digest[:12]}"
    return base


def assign_identities(doc_id: str, chunks: list[Chunk]) -> list[Identified]:
    seen: set[str] = set()
    identified: list[Identified] = []
    for chunk in chunks:
        digest = content_hash(chunk.source)
        key = identity_key(chunk, digest)
        candidate, number = key, 1
        while point_id(doc_id, candidate, chunk.part_index) in seen:
            number += 1
            candidate = f"{key}~{number}"
        chunk_id = point_id(doc_id, candidate, chunk.part_index)
        seen.add(chunk_id)
        identified.append(Identified(chunk, candidate, chunk_id, digest))
    return identified
