"""The Qdrant `documents` collection (spec 12.3, 6.4, 7).

One collection holds a named dense vector (768 dimensions, cosine) and a named sparse vector
(Qdrant applies IDF), plus payload indexes on every field a search filters on or looks chunks
up by. `ensure_documents_collection` creates what is missing and refuses to change what is
already there. It runs when the owner asks for it, on the admin machine:

    uv run python -m app.vector.documents --ensure

It reads QDRANT_URL and QDRANT_API_KEY from the shell, prints names and status only, and is
never run by the application itself or by the tests against a real cluster.
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field

from qdrant_client import QdrantClient, models

from app.gateway.embeddings import DIMENSIONS, running_on_render
from app.isolation import is_local_url
from app.store_errors import StoreUnavailable, vector_errors

COLLECTION = "documents"
DENSE_VECTOR = "dense"
SPARSE_VECTOR = "sparse"

# Fields the retrieval stage filters on (spec 7: access_role, effective_to, product_line,
# clause_type) and the ids the cache's read-time check and the delete flow look chunks up by.
# The other payload fields of 6.4 are stored but not indexed.
PAYLOAD_INDEXES: tuple[tuple[str, models.PayloadSchemaType], ...] = (
    ("access_role", models.PayloadSchemaType.KEYWORD),
    ("effective_to", models.PayloadSchemaType.DATETIME),
    ("product_line", models.PayloadSchemaType.KEYWORD),
    ("clause_type", models.PayloadSchemaType.KEYWORD),
    ("doc_id", models.PayloadSchemaType.KEYWORD),
    ("chunk_id", models.PayloadSchemaType.KEYWORD),
    ("content_type", models.PayloadSchemaType.KEYWORD),
)


class VectorConfigError(Exception):
    """A setting is wrong. The message names the variable, never a value."""


class CollectionMismatch(Exception):
    """The collection exists but is not shaped as the spec says. Nothing was changed."""


@dataclass
class EnsureResult:
    created: bool
    indexes_created: list[str] = field(default_factory=list)


def make_client(
    url: str | None,
    api_key: str | None = None,
    *,
    timeout: int = 10,
    env: Mapping[str, str] | None = None,
) -> QdrantClient:
    """A Qdrant client. An empty URL is an error, so an unset variable can never turn into a
    silently empty index. ":memory:" is for tests and development and is refused on Render."""
    address = (url or "").strip()
    if not address:
        raise VectorConfigError("QDRANT_URL is not set")
    if address == ":memory:":
        if running_on_render(env):
            raise VectorConfigError("QDRANT_URL must not be :memory: on Render")
        return QdrantClient(":memory:")
    if not is_local_url(address) and not address.lower().startswith("https://"):
        raise VectorConfigError("QDRANT_URL must start with https:// for a remote host")
    # check_compatibility=False: building the client must not make a network call of its own, so
    # an unreachable Qdrant surfaces as VectorStoreUnavailable on the first real call.
    return QdrantClient(
        url=address,
        api_key=(api_key or "").strip() or None,
        timeout=timeout,
        check_compatibility=False,
    )


def client_from_env(env: Mapping[str, str] | None = None) -> QdrantClient:
    env = os.environ if env is None else env
    return make_client(env.get("QDRANT_URL"), env.get("QDRANT_API_KEY"), env=env)


def _check_shape(info: models.CollectionInfo) -> None:
    params = info.config.params
    dense = params.vectors.get(DENSE_VECTOR) if isinstance(params.vectors, dict) else None
    if dense is None:
        raise CollectionMismatch(f"collection '{COLLECTION}' has no named vector '{DENSE_VECTOR}'")
    if dense.size != DIMENSIONS:
        raise CollectionMismatch(
            f"collection '{COLLECTION}' has {dense.size} dimensions, the spec says {DIMENSIONS}; "
            "vectors of different sizes cannot be mixed, so re-create it deliberately"
        )
    if dense.distance != models.Distance.COSINE:
        raise CollectionMismatch(f"collection '{COLLECTION}' does not use cosine distance")
    sparse = (params.sparse_vectors or {}).get(SPARSE_VECTOR)
    if sparse is None:
        raise CollectionMismatch(
            f"collection '{COLLECTION}' has no sparse vector '{SPARSE_VECTOR}'"
        )
    if sparse.modifier != models.Modifier.IDF:
        raise CollectionMismatch(f"sparse vector '{SPARSE_VECTOR}' does not apply IDF")


def ensure_documents_collection(client: QdrantClient) -> EnsureResult:
    """Create the collection and its payload indexes if missing; verify them if present.

    Safe to run again. A collection with a different shape is never touched: the function raises
    CollectionMismatch. If Qdrant cannot be reached it raises VectorStoreUnavailable.
    """
    with vector_errors():
        created = not client.collection_exists(COLLECTION)
        if created:
            client.create_collection(
                COLLECTION,
                vectors_config={
                    DENSE_VECTOR: models.VectorParams(
                        size=DIMENSIONS, distance=models.Distance.COSINE
                    )
                },
                sparse_vectors_config={
                    SPARSE_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF)
                },
            )
            existing: dict[str, models.PayloadIndexInfo] = {}
        else:
            info = client.get_collection(COLLECTION)
            _check_shape(info)
            existing = dict(info.payload_schema or {})
        indexes_created: list[str] = []
        for name, schema in PAYLOAD_INDEXES:
            present = existing.get(name)
            if present is not None:
                if present.data_type != schema:
                    raise CollectionMismatch(f"payload index '{name}' has a different type")
                continue
            with warnings.catch_warnings():
                # In-memory mode keeps no indexes and says so; the message is not news here.
                warnings.filterwarnings("ignore", message="Payload indexes have no effect")
                client.create_payload_index(COLLECTION, name, field_schema=schema)
            indexes_created.append(name)
    return EnsureResult(created=created, indexes_created=indexes_created)


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.vector.documents",
        description="Create the Qdrant 'documents' collection and its payload indexes.",
    )
    parser.add_argument(
        "--ensure", action="store_true", help="create what is missing (safe to run again)"
    )
    args = parser.parse_args(argv)
    if not args.ensure:
        print(__doc__)
        print("Nothing was done. Add --ensure to create the collection.")
        return 0
    try:
        result = ensure_documents_collection(client_from_env(env))
    except (VectorConfigError, CollectionMismatch) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except StoreUnavailable as error:
        print(f"error: Qdrant could not be reached ({error.cause})", file=sys.stderr)
        return 1
    print(f"collection '{COLLECTION}': {'created' if result.created else 'already exists'}")
    print(f"payload indexes created: {', '.join(result.indexes_created) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
