"""Document embeddings for ingestion, through the gateway's embedding client (spec 6.5).

The gateway tries key A, then key B. If both fail, ingestion stops (the error is raised and the
index is untouched). The development-only hash embedder is refused here unless the caller
explicitly allows it, and the command line allows it only when every store is local.
"""

from __future__ import annotations

from typing import Any

from app.gateway.embeddings import DevEmbedderForbidden
from app.ingest.index import Embed
from app.ingest.runner import LoopRunner

# How long one batch may wait for room in our own rate limiter before the step is skipped.
INGEST_MAX_WAIT_SECONDS = 90.0


def make_embedder(
    gateway: Any,
    runner: LoopRunner,
    *,
    allow_dev: bool = False,
    max_wait_s: float = INGEST_MAX_WAIT_SECONDS,
) -> Embed:
    def embed(texts: list[str], title: str | None) -> list[list[float]]:
        result = runner.run(
            gateway.embed(
                texts, task="document", titles=[title] * len(texts), max_wait_s=max_wait_s
            )
        )
        if result.is_dev_fallback and not allow_dev:
            raise DevEmbedderForbidden("development vectors must never be indexed")
        return result.vectors

    return embed
