"""Embedding helpers (spec 6.5, gemini-embedding-2 at 768 dimensions).

gemini-embedding-2 takes no task_type field: query and document are signalled by a
text prefix (Google's documentation). Vectors from different embedding models are
not comparable, so nothing from gemini-embedding-001 may be mixed in.

The hash-based embedder is for development only. It refuses to run on Render, or when
any database, Qdrant or cache URL is remote, and must never index or retrieve in production.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
from collections.abc import Mapping

from app.isolation import remote_variables

DIMENSIONS = 768
MAX_INPUT_TOKENS = 8_192
BATCH_SIZE = 16  # the documentation states no batch limit [unverified]; kept small

QUERY_PREFIX = "task: search result | query: "


class DevEmbedderForbidden(RuntimeError):
    """The development-only embedder was asked to run where it must not."""


def format_query(text: str) -> str:
    return f"{QUERY_PREFIX}{text}"


def format_document(text: str, title: str | None = None) -> str:
    # The documentation shows "title: {title} | text: {content}"; "none" for no title is an
    # assumption.
    return f"title: {title or 'none'} | text: {text}"


def norm(vector: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


def normalise(vector: list[float], tolerance: float = 1e-3) -> list[float]:
    """Return the vector with unit length. Already-normalised vectors are left alone."""
    length = norm(vector)
    if length == 0:
        raise ValueError("zero vector")
    if abs(length - 1.0) <= tolerance:
        return vector
    return [x / length for x in vector]


def vectors_ok(vectors: list[list[float]], count: int, dimensions: int = DIMENSIONS) -> bool:
    """True only if there is one usable vector per input: right size, finite numbers, not zero.

    A vector that fails this must never be returned or stored.
    """
    if len(vectors) != count:
        return False
    for vector in vectors:
        if len(vector) != dimensions or not all(math.isfinite(x) for x in vector):
            return False
        if norm(vector) == 0:
            return False
    return True


def running_on_render(env: Mapping[str, str] | None = None) -> bool:
    """Render sets RENDER=true on its services [unverified in this session]."""
    env = os.environ if env is None else env
    return (env.get("RENDER") or "").strip().lower() in ("1", "true", "yes")


def dev_embedder_blockers(env: Mapping[str, str] | None = None) -> list[str]:
    """Names (never values) of the reasons the hash embedder must not run here.

    It is refused on Render and whenever any database, Qdrant or cache URL points at a remote
    host: on the admin machine a real index may be one URL away, and fake vectors must never
    reach it. The check is code, not a comment.
    """
    names = ["RENDER"] if running_on_render(env) else []
    return names + remote_variables(env)


class HashEmbedder:
    """Deterministic fake vectors from a hash. Development only."""

    model = "hash-dev"

    def __init__(self, env: Mapping[str, str] | None = None, dimensions: int = DIMENSIONS):
        blockers = dev_embedder_blockers(env)
        if blockers:
            raise DevEmbedderForbidden(
                "the hash embedder must never run in production or next to a remote store: "
                + ", ".join(blockers)
            )
        self._dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        values: list[float] = []
        counter = 0
        while len(values) < self._dimensions:
            digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            values.extend(v / 2**31 - 1.0 for v in struct.unpack("<8I", digest))
            counter += 1
        return normalise(values[: self._dimensions])
