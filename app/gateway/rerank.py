"""Rerank chain (spec 4.1 step 10): Cohere, then Jina, then a lexical fallback.

Cohere and Jina are called with plain HTTP through httpx, inside the gateway
module, with the same key pools, limiter and breaker as every other call.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.gateway.errors import (
    CallTimeout,
    ProviderUnavailable,
    SchemaSurprise,
    status_to_error,
)

COHERE_URL = "https://api.cohere.com/v2/rerank"
JINA_URL = "https://api.jina.ai/v1/rerank"
URLS = {"cohere": COHERE_URL, "jina": JINA_URL}

_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class RerankItem:
    index: int  # position in the documents that were passed in
    score: float


@dataclass
class RerankResult:
    items: list[RerankItem]  # best first
    reranker: str  # cohere, jina or lexical
    model: str | None = None
    key_fingerprint: str | None = None
    fallback_reason: str | None = None
    degraded: bool = False  # True when the lexical fallback was used (the UI says so)
    input_tokens: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)


def lexical_scores(query: str, documents: list[str]) -> list[float]:
    """A small BM25-style word-overlap score. No network, no model."""
    docs = [_WORD.findall(d.lower()) for d in documents]
    terms = set(_WORD.findall(query.lower()))
    if not docs or not terms:
        return [0.0] * len(documents)
    average = sum(len(d) for d in docs) / len(docs) or 1.0
    scores = []
    for doc in docs:
        score = 0.0
        for term in terms:
            tf = doc.count(term)
            if not tf:
                continue
            df = sum(1 for d in docs if term in d)
            idf = math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))
            score += idf * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * len(doc) / average))
        scores.append(score)
    return scores


def top_items(scores: list[float], top_n: int | None) -> list[RerankItem]:
    ranked = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
    if top_n is not None:
        ranked = ranked[:top_n]
    return [RerankItem(i, scores[i]) for i in ranked]


async def http_rerank(
    provider: str,
    *,
    model: str,
    query: str,
    documents: list[str],
    top_n: int | None,
    api_key: str,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[RerankItem]:
    """Call Cohere or Jina. Both take model, query, documents and top_n."""
    body: dict[str, Any] = {"model": model, "query": query, "documents": documents}
    if top_n is not None:
        body["top_n"] = top_n
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            response = await client.post(URLS[provider], json=body, headers=headers)
    except httpx.TimeoutException:
        raise CallTimeout(status=408) from None
    except httpx.HTTPError:
        raise ProviderUnavailable() from None
    if response.status_code != 200:
        raise status_to_error(response.status_code, response.headers)
    try:
        results = response.json()["results"]
        items = [RerankItem(int(r["index"]), float(r["relevance_score"])) for r in results]
    except (ValueError, KeyError, TypeError):
        raise SchemaSurprise() from None
    if any(not 0 <= item.index < len(documents) for item in items):
        raise SchemaSurprise()
    return sorted(items, key=lambda item: -item.score)
