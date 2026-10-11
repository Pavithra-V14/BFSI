"""The Qdrant `documents` collection (spec 12.3, 6.4, 7).

One collection holds a named dense vector (768 dimensions, cosine) and a named sparse vector
(Qdrant applies IDF), plus payload indexes on every field a search filters on or looks chunks
up by. `ensure_documents_collection` creates what is missing and refuses to change what is
already there. It runs when the owner asks for it, on the admin machine:

    uv run python -m app.vector.documents --ensure

It reads QDRANT_URL and QDRANT_API_KEY from the shell, prints names and status only, and is
never run by the application itself or by the tests against a real cluster.

The `effective_to` rule (owner decision, spec 6.4 and 7): a chunk is active when `effective_to`
is empty (missing or null), and inactive as soon as it holds any value. There are no future
dates: a chunk is never "active until a date", and ingestion sets `effective_to` at the moment
a chunk is superseded or removed. `ACTIVE_ONLY` is the search filter that expresses the rule and
`chunk_is_active` is the same rule for a payload fetched for the cache's read-time check.

What may be stored is enforced in code: `validate_effective_to` accepts only None (or no field)
or a complete ISO 8601 date and time with a UTC offset that is not in the future. An empty string,
a date without a time, a time without an offset, an unreadable string or a future moment is
refused. Ingestion must pass every payload through `checked_payload` before it writes to Qdrant.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from qdrant_client import QdrantClient, models

from app.gateway.embeddings import DIMENSIONS, running_on_render
from app.isolation import is_local_url
from app.store_errors import StoreRejected, StoreUnavailable, call_vector

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


# "Only active chunks" (spec 7): effective_to is empty. IsEmpty matches a missing field, a null
# and an empty list, and nothing that holds a value, however far in the future.
ACTIVE_ONLY = models.Filter(
    must=[models.IsEmptyCondition(is_empty=models.PayloadField(key="effective_to"))]
)


def chunk_is_active(payload: Mapping[str, object] | None) -> bool:
    """The same rule for one payload: active only if effective_to is empty (absent, null or an
    empty list, exactly what the ACTIVE_ONLY filter matches). Any value at all, an empty string
    included, means inactive."""
    value = (payload or {}).get("effective_to")
    return value is None or (isinstance(value, list) and not value)


_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATE_AND_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}")  # the extended ISO 8601 form


class InvalidEffectiveTo(ValueError):
    """A value that must never be stored in effective_to. The message names the problem only."""


def validate_effective_to(value: object, *, now: datetime | None = None) -> str | None:
    """Return the value to store: None, or a complete ISO 8601 date and time with an offset.

    Refused (InvalidEffectiveTo): an empty or blank string, a date without a time, a time with no
    offset, anything that does not parse, anything that is not a string, and any moment after
    `now` (there are no future dates: a chunk is made inactive when it is superseded or removed).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidEffectiveTo("effective_to must be null or an ISO 8601 date and time")
    text = value.strip()
    if not text:
        raise InvalidEffectiveTo("effective_to must not be an empty string (use null)")
    if _DATE_ONLY.fullmatch(text):
        raise InvalidEffectiveTo("effective_to needs a time as well as a date")
    if not _DATE_AND_TIME.match(text):
        raise InvalidEffectiveTo("effective_to is not a valid ISO 8601 date and time")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise InvalidEffectiveTo("effective_to is not a valid ISO 8601 date and time") from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise InvalidEffectiveTo("effective_to needs a UTC offset, for example Z or +00:00")
    if moment > (now or datetime.now(UTC)):
        raise InvalidEffectiveTo("effective_to must not be in the future")
    return text


def checked_payload(payload: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """A copy of a chunk payload that is safe to write: effective_to is validated, and an absent
    field is written as an explicit null so that "active" is always visible in the data."""
    checked = dict(payload)
    checked["effective_to"] = validate_effective_to(payload.get("effective_to"), now=now)
    return checked


def checked_update(payload: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """A copy of a PARTIAL payload (for set_payload) that is safe to write. `effective_to` is
    validated when the update carries it and is never added when it does not, so refreshing the
    pages of a held-back chunk can never make it active (checked_payload would add a null)."""
    checked = dict(payload)
    if "effective_to" in checked:
        checked["effective_to"] = validate_effective_to(checked["effective_to"], now=now)
    return checked


@dataclass(frozen=True)
class SparseCheck:
    """How many stored chunks hold a sparse vector made by a different tokenizer version."""

    total: int
    out_of_date: int

    @property
    def ok(self) -> bool:
        return self.out_of_date == 0


def sparse_check(client: QdrantClient, collection: str = COLLECTION) -> SparseCheck:
    """Counts only: all stored chunks, and those whose `sparse_version` is missing or differs from
    the code's `SPARSE_VERSION`. Used by the status command, the pipeline's start-up check and the
    smoke script."""
    from app.vector.sparse import SPARSE_VERSION

    current = models.FieldCondition(
        key="sparse_version", match=models.MatchValue(value=SPARSE_VERSION)
    )

    def work() -> SparseCheck:
        total = client.count(collection, exact=True).count
        stale = client.count(
            collection, count_filter=models.Filter(must_not=[current]), exact=True
        ).count
        return SparseCheck(int(total), int(stale))

    return call_vector(work)


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
    if running_on_render(env) and is_local_url(address):
        raise VectorConfigError("QDRANT_URL must not point at this machine on Render")
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


def _check_shape(info: models.CollectionInfo, collection: str = COLLECTION) -> None:
    params = info.config.params
    dense = params.vectors.get(DENSE_VECTOR) if isinstance(params.vectors, dict) else None
    if dense is None:
        raise CollectionMismatch(f"collection '{collection}' has no named vector '{DENSE_VECTOR}'")
    if dense.size != DIMENSIONS:
        raise CollectionMismatch(
            f"collection '{collection}' has {dense.size} dimensions, the spec says {DIMENSIONS}; "
            "vectors of different sizes cannot be mixed, so re-create it deliberately"
        )
    if dense.distance != models.Distance.COSINE:
        raise CollectionMismatch(f"collection '{collection}' does not use cosine distance")
    sparse = (params.sparse_vectors or {}).get(SPARSE_VECTOR)
    if sparse is None:
        raise CollectionMismatch(
            f"collection '{collection}' has no sparse vector '{SPARSE_VECTOR}'"
        )
    if sparse.modifier != models.Modifier.IDF:
        raise CollectionMismatch(f"sparse vector '{SPARSE_VECTOR}' does not apply IDF")


def ensure_documents_collection(client: QdrantClient, collection: str = COLLECTION) -> EnsureResult:
    """Create the collection and its payload indexes if missing; verify them if present.

    Safe to run again. `collection` is only ever changed by the owner-run Qdrant check, which uses a
    throwaway name. A collection with a different shape is never touched: the function raises
    CollectionMismatch. If Qdrant cannot be reached it raises VectorStoreUnavailable, and if it
    refuses the key or the certificate, StoreRejected.
    """

    def work() -> EnsureResult:
        created = not client.collection_exists(collection)
        if created:
            client.create_collection(
                collection,
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
            info = client.get_collection(collection)
            _check_shape(info, collection)
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
                client.create_payload_index(collection, name, field_schema=schema)
            indexes_created.append(name)
        return EnsureResult(created=created, indexes_created=indexes_created)

    return call_vector(work)


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.vector.documents",
        description="Create the Qdrant 'documents' collection and its payload indexes.",
    )
    parser.add_argument(
        "--ensure", action="store_true", help="create what is missing (safe to run again)"
    )
    parser.add_argument(
        "--collection",
        default=COLLECTION,
        help="evaluation only: the test collection to create (for example documents_eval)",
    )
    args = parser.parse_args(argv)
    collection = COLLECTION
    if args.collection != COLLECTION:
        from app.eval.constants import EvalConfigError, check_eval_collection

        try:
            collection = check_eval_collection(args.collection)
        except EvalConfigError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
    if not args.ensure:
        print(__doc__)
        print("Nothing was done. Add --ensure to create the collection.")
        return 0
    try:
        result = ensure_documents_collection(client_from_env(env), collection)
    except (VectorConfigError, CollectionMismatch) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except StoreUnavailable as error:
        print(f"error: Qdrant could not be reached ({error.cause})", file=sys.stderr)
        return 1
    except StoreRejected as error:
        print(
            f"error: Qdrant rejected the request ({error.reason}); check QDRANT_API_KEY",
            file=sys.stderr,
        )
        return 1
    print(f"collection '{collection}': {'created' if result.created else 'already exists'}")
    print(f"payload indexes created: {', '.join(result.indexes_created) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
