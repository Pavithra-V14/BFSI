"""Shared helpers for the ingestion tests: in-memory Qdrant, fakeredis, SQLite and a fake embedder.

Nothing here touches a network. "Tokens" are whitespace-separated words, so sizes are exact.
"""

from __future__ import annotations

import hashlib
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import fakeredis
from qdrant_client import QdrantClient, models

from app.cache.store import CacheStore
from app.db.connection import connect
from app.db.migrate import run
from app.gateway.embeddings import DIMENSIONS
from app.ingest.index import IngestContext, ingest_document
from app.ingest.model import DocRequest, Element, FailedPage, ParsedDocument
from app.vector.documents import ACTIVE_ONLY, COLLECTION, ensure_documents_collection

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
START = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def words(text: str) -> int:
    return len(text.split())


def title(text: str, page: int = 1) -> Element:
    return Element("title", text, page)


def para(text: str, page: int = 1) -> Element:
    return Element("text", text, page)


def table(rows: list[list[str]], page: int = 1) -> Element:
    return Element("table", "", page, rows=tuple(tuple(r) for r in rows))


def image(data: bytes, page: int = 1) -> Element:
    return Element("image", "", page, image=data)


def parsed(*elements: Element, pages: int = 3, failed: tuple[int, ...] = ()) -> ParsedDocument:
    return ParsedDocument(
        elements=list(elements),
        page_count=pages,
        failed_pages=[FailedPage(n, "ParseError") for n in failed],
    )


def filler(count: int, seed: str = "w") -> str:
    """`count` words in sentences of ten words, each starting with a capital, so a long clause
    can split at sentence ends."""
    out = []
    for n in range(count):
        word = f"{seed}{n}"
        out.append((word.capitalize() if n % 10 == 0 else word) + ("." if n % 10 == 9 else ""))
    return " ".join(out)


def sample_elements() -> list[Element]:
    return [
        title("Section 1. Definitions"),
        para("Policy means this contract. Insured means the person covered."),
        title("Section 2. Waiting periods"),
        para("A waiting period of 30 days applies to illness claims."),
        title("Section 3. Exclusions"),
        para("War and nuclear risks are excluded. Self harm is excluded."),
    ]


class FakeEmbedder:
    """Deterministic unit vectors from the text. Records every call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.titles: list[str | None] = []
        self.fail = False

    def __call__(self, texts: list[str], title: str | None) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embedding service down")
        self.calls.append(list(texts))
        self.titles.append(title)
        return [self.vector(text) for text in texts]

    @staticmethod
    def vector(text: str) -> list[float]:
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
        rng = random.Random(seed)
        raw = [rng.random() - 0.5 for _ in range(DIMENSIONS)]
        length = sum(x * x for x in raw) ** 0.5
        return [x / length for x in raw]

    @property
    def texts(self) -> list[str]:
        return [text for call in self.calls for text in call]


class Rig:
    """One ingestion setup: its own database file, cache and in-memory Qdrant."""

    def __init__(self, tmp_path: Path, enrich=None) -> None:
        self.url = f"sqlite:///{tmp_path / 'ingest.db'}"
        run(self.url, MIGRATIONS, log=lambda line: None)
        self.db = connect(self.url)
        self.qdrant = QdrantClient(":memory:")
        ensure_documents_collection(self.qdrant)
        self.redis = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
        self.events: list[tuple] = []
        self.cache = CacheStore(self.redis, on_event=lambda name, fields: self.events.append(name))
        self.embedder = FakeEmbedder()
        self.moment = START
        self.ctx = IngestContext(
            qdrant=self.qdrant,
            cache=self.cache,
            db=self.db,
            embed=self.embedder,
            count=words,
            lock_dir=tmp_path / "locks",
            enrich=enrich,
            now=lambda: self.moment,
        )

    def close(self) -> None:
        self.db.close()

    def request(self, **overrides) -> DocRequest:
        values = {
            "doc_id": "doc-a",
            "source": "a.pdf",
            "title": "Policy A",
            "doc_type": "policy",
            "access_role": "customer",
            "product_line": "insurance",
            "effective_date": "2026-10-01",
        }
        values.update(overrides)
        return DocRequest(**values)

    def ingest(self, document: ParsedDocument, **overrides):
        self.moment += timedelta(minutes=1)
        return ingest_document(self.ctx, self.request(**overrides), document)

    def points(self, doc_id: str = "doc-a", vectors: bool = False) -> dict[str, dict]:
        found, offset = {}, None
        flt = models.Filter(
            must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]
        )
        while True:
            batch, offset = self.qdrant.scroll(
                COLLECTION, scroll_filter=flt, limit=100, offset=offset, with_vectors=vectors
            )
            for point in batch:
                found[str(point.id)] = {"payload": point.payload, "vector": point.vector}
            if offset is None:
                return found

    def active(self, doc_id: str = "doc-a") -> dict[str, dict]:
        keep = {
            p.id for p in self.qdrant.scroll(COLLECTION, scroll_filter=ACTIVE_ONLY, limit=500)[0]
        }
        return {k: v for k, v in self.points(doc_id).items() if k in keep}

    def corpus_version(self) -> int | None:
        return self.cache.corpus_version()
