"""The Qdrant `documents` collection, in Qdrant's in-memory mode (no network, no cluster)."""

import pytest
from qdrant_client import QdrantClient, models

from app.gateway.embeddings import DIMENSIONS
from app.store_errors import VectorStoreUnavailable
from app.vector import documents
from app.vector.documents import (
    COLLECTION,
    DENSE_VECTOR,
    PAYLOAD_INDEXES,
    SPARSE_VECTOR,
    CollectionMismatch,
    VectorConfigError,
    client_from_env,
    ensure_documents_collection,
    make_client,
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
