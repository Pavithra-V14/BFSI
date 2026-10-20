"""The Qdrant `documents` collection, in Qdrant's in-memory mode (no network, no cluster)."""

from datetime import UTC, datetime

import pytest
from qdrant_client import QdrantClient, models

from app.gateway.embeddings import DIMENSIONS
from app.store_errors import VectorStoreUnavailable
from app.vector import documents
from app.vector.documents import (
    ACTIVE_ONLY,
    COLLECTION,
    DENSE_VECTOR,
    PAYLOAD_INDEXES,
    SPARSE_VECTOR,
    CollectionMismatch,
    InvalidEffectiveTo,
    VectorConfigError,
    checked_payload,
    chunk_is_active,
    client_from_env,
    ensure_documents_collection,
    make_client,
    validate_effective_to,
)


@pytest.fixture
def client():
    return QdrantClient(":memory:")


def test_the_collection_has_a_768_dense_cosine_vector_and_a_sparse_idf_vector(client):
    result = ensure_documents_collection(client)
    assert result.created
    params = client.get_collection(COLLECTION).config.params
    dense = params.vectors[DENSE_VECTOR]
    assert dense.size == DIMENSIONS == 768 and dense.distance == models.Distance.COSINE
    assert params.sparse_vectors[SPARSE_VECTOR].modifier == models.Modifier.IDF


def test_payload_indexes_cover_every_filter_field_of_section_7():
    fields = {name for name, _ in PAYLOAD_INDEXES}
    assert {"access_role", "effective_to", "product_line", "clause_type"} <= fields
    assert {"doc_id", "chunk_id"} <= fields  # the cache check and the delete flow look up by id
    types = dict(PAYLOAD_INDEXES)
    # the start-up check and the sparse refresh filter on it (a number in the payload)
    assert types["sparse_version"] == models.PayloadSchemaType.INTEGER
    assert types["effective_to"] == models.PayloadSchemaType.DATETIME
    assert types["access_role"] == models.PayloadSchemaType.KEYWORD


def test_every_planned_index_is_requested_once(client, monkeypatch):
    asked = []
    original = client.create_payload_index

    def spy(collection_name, field_name, **kwargs):
        asked.append((collection_name, field_name, kwargs["field_schema"]))
        return original(collection_name, field_name, **kwargs)

    monkeypatch.setattr(client, "create_payload_index", spy)
    result = ensure_documents_collection(client)
    assert asked == [(COLLECTION, name, schema) for name, schema in PAYLOAD_INDEXES]
    assert result.indexes_created == [name for name, _ in PAYLOAD_INDEXES]


def test_running_it_again_changes_nothing_and_does_not_fail(client):
    ensure_documents_collection(client)
    again = ensure_documents_collection(client)
    assert not again.created
    assert client.collection_exists(COLLECTION)


def test_a_collection_with_the_wrong_dimension_is_refused_and_left_alone(client):
    client.create_collection(
        COLLECTION,
        vectors_config={
            DENSE_VECTOR: models.VectorParams(size=3072, distance=models.Distance.COSINE)
        },
        sparse_vectors_config={
            SPARSE_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF)
        },
    )
    with pytest.raises(CollectionMismatch, match="3072 dimensions"):
        ensure_documents_collection(client)
    assert client.get_collection(COLLECTION).config.params.vectors[DENSE_VECTOR].size == 3072


@pytest.mark.parametrize(
    ("vectors", "sparse", "message"),
    [
        (
            {"dense": models.VectorParams(size=768, distance=models.Distance.DOT)},
            {"sparse": models.SparseVectorParams(modifier=models.Modifier.IDF)},
            "cosine",
        ),
        (
            {"other": models.VectorParams(size=768, distance=models.Distance.COSINE)},
            {"sparse": models.SparseVectorParams(modifier=models.Modifier.IDF)},
            "named vector 'dense'",
        ),
        (
            {"dense": models.VectorParams(size=768, distance=models.Distance.COSINE)},
            {"other": models.SparseVectorParams(modifier=models.Modifier.IDF)},
            "sparse vector 'sparse'",
        ),
        (
            {"dense": models.VectorParams(size=768, distance=models.Distance.COSINE)},
            {"sparse": models.SparseVectorParams()},
            "IDF",
        ),
    ],
)
def test_other_wrong_shapes_are_refused(client, vectors, sparse, message):
    client.create_collection(COLLECTION, vectors_config=vectors, sparse_vectors_config=sparse)
    with pytest.raises(CollectionMismatch, match=message):
        ensure_documents_collection(client)


def test_an_existing_index_of_the_wrong_type_is_refused(client, monkeypatch):
    ensure_documents_collection(client)
    wrong = models.PayloadIndexInfo(data_type=models.PayloadSchemaType.INTEGER, points=0)
    info = client.get_collection(COLLECTION)
    info.payload_schema = {"access_role": wrong}
    monkeypatch.setattr(client, "get_collection", lambda name: info)
    with pytest.raises(CollectionMismatch, match="access_role"):
        ensure_documents_collection(client)


def test_a_correct_existing_index_is_not_created_again(client, monkeypatch):
    ensure_documents_collection(client)
    info = client.get_collection(COLLECTION)
    info.payload_schema = {
        name: models.PayloadIndexInfo(data_type=schema, points=0)
        for name, schema in PAYLOAD_INDEXES
    }
    monkeypatch.setattr(client, "get_collection", lambda name: info)
    monkeypatch.setattr(
        client,
        "create_payload_index",
        lambda *a, **k: pytest.fail("an index that exists was created again"),
    )
    assert ensure_documents_collection(client).indexes_created == []


def test_the_in_memory_notice_about_indexes_is_not_shown(client, recwarn):
    ensure_documents_collection(client)
    assert not [w for w in recwarn if "Payload indexes" in str(w.message)]


# ---- the client factory ---------------------------------------------------------------


def test_an_unset_url_is_an_error_not_an_empty_index():
    for value in (None, "", "   "):
        with pytest.raises(VectorConfigError, match="QDRANT_URL is not set"):
            make_client(value)
    with pytest.raises(VectorConfigError):
        client_from_env({})


def test_memory_mode_is_allowed_except_on_render():
    assert make_client(":memory:", env={}).collection_exists(COLLECTION) is False
    with pytest.raises(VectorConfigError, match="Render"):
        make_client(":memory:", env={"RENDER": "true"})


def test_a_remote_url_must_use_https_and_the_error_never_shows_it():
    url = "http://qdrant.example.test:6333"
    with pytest.raises(VectorConfigError) as caught:
        make_client(url, "key-not-real")
    assert "https://" in str(caught.value)
    assert url not in str(caught.value) and "key-not-real" not in str(caught.value)


def test_a_local_http_url_and_a_remote_https_url_build_a_client_without_connecting():
    # Building the client opens no connection, so this needs no server.
    assert make_client("http://localhost:6333") is not None
    assert make_client("https://abc.cloud.qdrant.example.test:6333", "key-not-real") is not None


# ---- the command the owner runs -----------------------------------------------------------


def test_the_command_does_nothing_without_ensure(capsys):
    assert documents.main([], env={"QDRANT_URL": ":memory:"}) == 0
    assert "Nothing was done" in capsys.readouterr().out


def test_ensure_creates_the_collection_and_prints_names_only(capsys, monkeypatch):
    shared = QdrantClient(":memory:")
    monkeypatch.setattr(documents, "client_from_env", lambda env=None: shared)
    assert documents.main(["--ensure"], env={}) == 0
    out = capsys.readouterr().out
    assert "collection 'documents': created" in out and "access_role" in out
    assert documents.main(["--ensure"], env={}) == 0
    assert "already exists" in capsys.readouterr().out


def test_the_command_reports_a_missing_url_and_a_mismatch_without_a_traceback(capsys, monkeypatch):
    assert documents.main(["--ensure"], env={}) == 1
    assert "QDRANT_URL is not set" in capsys.readouterr().err
    shared = QdrantClient(":memory:")
    shared.create_collection(
        COLLECTION,
        vectors_config={DENSE_VECTOR: models.VectorParams(size=5, distance=models.Distance.COSINE)},
        sparse_vectors_config={
            SPARSE_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF)
        },
    )
    monkeypatch.setattr(documents, "client_from_env", lambda env=None: shared)
    assert documents.main(["--ensure"], env={}) == 1
    assert "5 dimensions" in capsys.readouterr().err


# ---- Qdrant down (spec 11.1) ---------------------------------------------------------------


def test_a_qdrant_that_cannot_be_reached_is_a_typed_unavailable_error():
    # Port 1 on this machine has nothing listening: a real connection failure, no network used.
    dead = QdrantClient(url="http://127.0.0.1:1", timeout=1, check_compatibility=False)
    with pytest.raises(VectorStoreUnavailable) as caught:
        ensure_documents_collection(dead)
    assert "temporarily unavailable" in str(caught.value)
    assert "127.0.0.1" not in str(caught.value) and caught.value.cause


def test_the_command_says_qdrant_could_not_be_reached_without_the_address(capsys, monkeypatch):
    dead = QdrantClient(url="http://127.0.0.1:1", timeout=1, check_compatibility=False)
    monkeypatch.setattr(documents, "client_from_env", lambda env=None: dead)
    assert documents.main(["--ensure"], env={}) == 1
    err = capsys.readouterr().err
    assert "could not be reached" in err and "127.0.0.1" not in err


# ---- the effective_to rule: empty means active, any value means inactive (spec 6.4, 7) ----------

# payload -> active? Any value at all, past or future, means inactive at once.
EFFECTIVE_TO_CASES = {
    "absent": ({"doc_id": "d-absent"}, True),
    "null": ({"doc_id": "d-null", "effective_to": None}, True),
    "past date": ({"doc_id": "d-past", "effective_to": "2020-01-01T00:00:00Z"}, False),
    "today": ({"doc_id": "d-today", "effective_to": "2026-10-06T00:00:00Z"}, False),
    # The validator refuses to store a future date; the filter still treats one as inactive.
    "future date": ({"doc_id": "d-future", "effective_to": "2999-01-01T00:00:00Z"}, False),
}


def _fill(client):
    ensure_documents_collection(client)
    points = [
        models.PointStruct(
            id=number,
            vector={
                DENSE_VECTOR: [1.0] + [0.0] * (DIMENSIONS - 1),
                SPARSE_VECTOR: models.SparseVector(indices=[number], values=[1.0]),
            },
            payload=payload,
        )
        for number, (payload, _) in enumerate(EFFECTIVE_TO_CASES.values(), start=1)
    ]
    client.upsert(COLLECTION, points=points)


def test_the_active_filter_keeps_only_chunks_whose_effective_to_is_empty(client):
    _fill(client)
    found, _ = client.scroll(COLLECTION, scroll_filter=ACTIVE_ONLY, limit=50)
    assert sorted(point.payload["doc_id"] for point in found) == ["d-absent", "d-null"]


def test_a_future_date_is_inactive_at_once_there_is_no_scheduled_end(client):
    _fill(client)
    found, _ = client.scroll(COLLECTION, scroll_filter=ACTIVE_ONLY, limit=50)
    ids = {point.payload["doc_id"] for point in found}
    assert "d-future" not in ids and "d-today" not in ids and "d-past" not in ids


@pytest.mark.parametrize("name", sorted(EFFECTIVE_TO_CASES))
def test_the_payload_check_agrees_with_the_search_filter(client, name):
    payload, active = EFFECTIVE_TO_CASES[name]
    assert chunk_is_active(payload) is active
    _fill(client)
    found, _ = client.scroll(COLLECTION, scroll_filter=ACTIVE_ONLY, limit=50)
    assert (payload["doc_id"] in {p.payload["doc_id"] for p in found}) is active


def test_chunk_is_active_handles_a_missing_payload_and_an_empty_list():
    assert chunk_is_active(None) is True
    assert chunk_is_active({}) is True
    assert chunk_is_active({"effective_to": []}) is True
    assert chunk_is_active({"effective_to": ["2020-01-01"]}) is False


def test_the_rule_is_written_down_in_the_module():
    text = " ".join(documents.__doc__.split())  # the docstring wraps lines
    assert "empty" in text and "no future dates" in text and "inactive" in text


# ---- what may be stored in effective_to is enforced in code -----------------------------------

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "2026-10-06T00:00:00Z",
        "2026-10-06T12:00:00+00:00",  # exactly now is allowed
        "2020-01-01T08:30:00+05:30",
        "2026-10-06T10:00:00.123456Z",
    ],
)
def test_null_or_a_complete_past_iso_moment_with_an_offset_may_be_stored(value):
    assert validate_effective_to(value, now=NOW) == value


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("", "empty string"),
        ("   ", "empty string"),
        ("2026-10-06", "needs a time"),  # a date without a time
        ("2026-10-06T00:00:00", "UTC offset"),  # a time without an offset
        ("yesterday", "valid ISO"),  # has a letter T in it, but is not a date
        ("20261006T120000Z", "valid ISO"),  # only the extended form is stored
        ("2026-13-45T00:00:00Z", "valid ISO"),
        ("2026-10-06T12:00:01Z", "future"),  # one second after now
        ("2999-01-01T00:00:00Z", "future"),
        (0, "ISO 8601"),
        (1_760_000_000, "ISO 8601"),
        ([], "ISO 8601"),
        (["2020-01-01T00:00:00Z"], "ISO 8601"),
        (True, "ISO 8601"),
    ],
)
def test_anything_else_is_refused_and_an_empty_string_is_never_stored(value, why):
    with pytest.raises(InvalidEffectiveTo, match=why):
        validate_effective_to(value, now=NOW)


def test_the_current_moment_is_the_default_limit():
    assert validate_effective_to("2020-01-01T00:00:00Z") == "2020-01-01T00:00:00Z"
    with pytest.raises(InvalidEffectiveTo, match="future"):
        validate_effective_to("2999-01-01T00:00:00Z")


def test_a_checked_payload_writes_an_explicit_null_for_an_active_chunk():
    assert checked_payload({"doc_id": "d"}, now=NOW) == {"doc_id": "d", "effective_to": None}
    assert checked_payload({"doc_id": "d", "effective_to": None}, now=NOW)["effective_to"] is None
    done = checked_payload({"effective_to": "2026-01-01T00:00:00Z"}, now=NOW)
    assert done["effective_to"] == "2026-01-01T00:00:00Z"


def test_a_checked_payload_refuses_an_empty_string_and_leaves_the_original_alone():
    original = {"doc_id": "d", "effective_to": ""}
    with pytest.raises(InvalidEffectiveTo):
        checked_payload(original, now=NOW)
    assert original == {"doc_id": "d", "effective_to": ""}  # not modified, not half-fixed


def test_a_checked_payload_is_active_exactly_when_the_filter_says_so(client):
    # What passes the check and is written to Qdrant is judged the same by both rules.
    ensure_documents_collection(client)
    stored = [
        checked_payload({"doc_id": "a"}, now=NOW),
        checked_payload({"doc_id": "b", "effective_to": None}, now=NOW),
        checked_payload({"doc_id": "c", "effective_to": "2025-05-05T00:00:00Z"}, now=NOW),
    ]
    points = [
        models.PointStruct(
            id=number,
            vector={
                DENSE_VECTOR: [1.0] + [0.0] * (DIMENSIONS - 1),
                SPARSE_VECTOR: models.SparseVector(indices=[number], values=[1.0]),
            },
            payload=payload,
        )
        for number, payload in enumerate(stored, start=1)
    ]
    client.upsert(COLLECTION, points=points)
    found, _ = client.scroll(COLLECTION, scroll_filter=ACTIVE_ONLY, limit=10)
    assert (
        {p.payload["doc_id"] for p in found}
        == {p["doc_id"] for p in stored if chunk_is_active(p)}
        == {"a", "b"}
    )


# ---- Render rules, rejected credentials ------------------------------------------------------


def test_a_local_qdrant_address_is_refused_on_render_like_the_cache_one():
    for address in ("http://localhost:6333", "localhost:6333", "http://127.0.0.1:6333"):
        with pytest.raises(VectorConfigError, match="Render"):
            make_client(address, env={"RENDER": "true"})
    # Off Render the same addresses are fine, and a remote https address is fine on Render.
    assert make_client("http://localhost:6333", env={}) is not None
    assert make_client("https://abc.cloud.qdrant.example.test:6333", env={"RENDER": "1"})


def test_the_command_reports_rejected_credentials_without_the_address(capsys, monkeypatch):
    from qdrant_client.http import exceptions as qdrant_exceptions

    class Rejecting:
        def collection_exists(self, name):
            raise qdrant_exceptions.UnexpectedResponse(401, "Unauthorized", b"", None)

    monkeypatch.setattr(documents, "client_from_env", lambda env=None: Rejecting())
    assert documents.main(["--ensure"], env={}) == 1
    err = capsys.readouterr().err
    assert "rejected the request (HTTP 401)" in err and "QDRANT_API_KEY" in err
    assert "could not be reached" not in err  # a bad key is not an outage


def test_a_name_clash_on_create_is_not_reported_as_an_outage(client, monkeypatch):
    from qdrant_client.http import exceptions as qdrant_exceptions

    def clash(*args, **kwargs):
        raise qdrant_exceptions.UnexpectedResponse(409, "Conflict", b"", None)

    monkeypatch.setattr(client, "create_collection", clash)
    with pytest.raises(qdrant_exceptions.UnexpectedResponse):
        ensure_documents_collection(client)
