"""Real-Qdrant check (Phase 5, first task). Off by default; the owner runs it by hand.

It answers three questions about a REAL Qdrant server that the in-memory mode cannot:
  1. does a DATETIME payload index on `effective_to` work together with the "active only"
     (IsEmpty) filter, so that a missing field and a null count as active and any value as not,
  2. does the one-call hybrid query (dense and sparse branches, fused with RRF) accept that
     filter on BOTH branches,
  3. are the sparse vectors and payload indexes created the way the spec says.

It never touches the real `documents` collection. It creates one collection with a unique
throw-away name (check_<random>), writes five tiny made-up points, queries them, and deletes the
collection in a finally block, whatever happens. It prints names and PASS/FAIL only.

Run it in your own terminal:
    uv run python scripts/qdrant_check.py --run
It reads QDRANT_URL and QDRANT_API_KEY from the shell (never from a file). Nothing happens
without --run.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qdrant_client import QdrantClient, models  # noqa: E402

from app.gateway.embeddings import DIMENSIONS  # noqa: E402
from app.vector import documents  # noqa: E402
from app.vector.sparse import document_vector, query_vector  # noqa: E402

PREFIX = "check_"


class RefusedCollection(ValueError):
    """The check was about to use a name that could be a real collection."""


def throwaway_name() -> str:
    return PREFIX + secrets.token_hex(6)


def guard_name(name: str) -> str:
    """Only a unique throw-away name is allowed: never `documents`, never anything else."""
    if name == documents.COLLECTION or not name.startswith(PREFIX) or len(name) <= len(PREFIX) + 8:
        raise RefusedCollection("the check only uses a unique throw-away collection name")
    return name


def line(name: str, status: str, **details: object) -> None:
    extra = " ".join(f"{key}={value}" for key, value in details.items())
    print(f"{name:<34} {status}" + (f"  {extra}" if extra else ""))


def _dense(seed: int) -> list[float]:
    vector = [0.0] * DIMENSIONS
    vector[seed % DIMENSIONS] = 1.0
    return vector


# (point number, payload, text). Active: 1 (null), 2 (field missing), 3 (empty list), 5 (staff).
POINTS: tuple[tuple[int, dict[str, object], str], ...] = (
    (1, {"effective_to": None, "access_role": "customer"}, "waiting period for claims"),
    (2, {"access_role": "customer"}, "waiting period for claims in hospital"),
    (3, {"effective_to": [], "access_role": "customer"}, "waiting period claims cover"),
    (4, {"effective_to": "2026-01-01T00:00:00+00:00", "access_role": "customer"}, "waiting period"),
    (5, {"effective_to": None, "access_role": "staff"}, "waiting period staff only"),
)


def run_check(
    client: QdrantClient,
    name: str,
    report: Callable[..., None] = line,
    *,
    expect_indexes: bool = True,
) -> int:
    """Create the throw-away collection, check, and always delete it. Returns the problem count.

    `expect_indexes` is False only for Qdrant's in-memory mode, which keeps no payload indexes."""
    guard_name(name)
    problems = 0

    def result(label: str, ok: bool, **details: object) -> None:
        nonlocal problems
        if not ok:
            problems += 1
        report(label, "PASS" if ok else "FAIL", **details)

    try:
        created = documents.ensure_documents_collection(client, name)
        result("collection created", created.created, indexes=len(created.indexes_created))
        info = client.get_collection(name)
        sparse = (info.config.params.sparse_vectors or {}).get(documents.SPARSE_VECTOR)
        result(
            "sparse vector uses IDF", sparse is not None and sparse.modifier == models.Modifier.IDF
        )
        schema = info.payload_schema or {}
        wanted = dict(documents.PAYLOAD_INDEXES)
        result(
            "payload indexes present",
            not expect_indexes
            or all(n in schema and schema[n].data_type == t for n, t in wanted.items()),
            count=len(schema),
        )
        client.upsert(
            name,
            wait=True,
            points=[
                models.PointStruct(
                    id=number,
                    vector={
                        documents.DENSE_VECTOR: _dense(number),
                        documents.SPARSE_VECTOR: document_vector(text),
                    },
                    payload=payload,
                )
                for number, payload, text in POINTS
            ],
        )
        active = client.scroll(name, scroll_filter=documents.ACTIVE_ONLY, limit=20)[0]
        ids = sorted(point.id for point in active)
        result("IsEmpty keeps null/missing/[]", ids == [1, 2, 3, 5], active_ids=ids)

        both = models.Filter(
            must=[
                *documents.ACTIVE_ONLY.must,
                models.FieldCondition(key="access_role", match=models.MatchValue(value="customer")),
            ]
        )
        hybrid = client.query_points(
            name,
            prefetch=[
                models.Prefetch(
                    query=_dense(1), using=documents.DENSE_VECTOR, filter=both, limit=10
                ),
                models.Prefetch(
                    query=query_vector("waiting period claims"),
                    using=documents.SPARSE_VECTOR,
                    filter=both,
                    limit=10,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=10,
        ).points
        found = sorted(point.id for point in hybrid)
        result("hybrid RRF, filter on both", found == [1, 2, 3], returned_ids=found)
    finally:
        try:
            client.delete_collection(name)
            report("throw-away collection deleted", "DONE")
        except Exception as error:  # report, never hide: the owner must remove it by hand
            report("throw-away collection deleted", "FAILED", error=type(error).__name__)
            problems += 1
    return problems


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Real-Qdrant check on a throw-away collection.")
    parser.add_argument("--run", action="store_true", help="run the check (REAL Qdrant)")
    args = parser.parse_args(argv)
    if not args.run:
        print(__doc__)
        print("Nothing was done. Add --run to check your Qdrant.")
        return 0
    env = os.environ if env is None else env
    try:
        client = documents.client_from_env(env)
        name = guard_name(throwaway_name())
        print(f"using a throw-away collection named {name}")
        problems = run_check(client, name)
    except documents.VectorConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # names only, never a URL or a key
        print(f"error: the check stopped ({type(error).__name__})", file=sys.stderr)
        return 1
    print("all checks passed" if not problems else f"{problems} check(s) failed")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
