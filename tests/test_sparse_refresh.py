"""Phase 9 (spec 6.6, 10.3 item 6): every chunk records the tokenizer version of its sparse vector;
a chunk made by another version only gets its sparse vector rewritten (no embedding call), with one
corpus-version bump at the end; status counts what is out of date; a start-up check warns."""

import pytest
import redis
from qdrant_client import models

from app.ingest import pending
from app.ingest.cli import Services, main
from app.ingest.index import refreshed_sparse_vector
from app.ingest.sparse_refresh import refresh_sparse_vectors
from app.ingest.status import describe, document_status
from app.pipeline.run import Pipeline
from app.vector import sparse
from app.vector.documents import COLLECTION, SPARSE_VECTOR, sparse_check
from tests.ingest_fixtures import Rig, para, parsed, sample_elements, title
from tests.pipeline_rig import PipelineRig


@pytest.fixture
def rig(tmp_path):
    made = Rig(tmp_path)
    yield made
    made.close()


def make_stale(rig, doc_id="doc-a", version=0):
    """Pretend an older tokenizer made the sparse vectors: wrong version, wrong vector."""
    ids = list(rig.points(doc_id))
    rig.qdrant.set_payload(COLLECTION, payload={"sparse_version": version}, points=ids)
    rig.qdrant.update_vectors(
        COLLECTION,
        points=[
            models.PointVectors(
                id=i, vector={SPARSE_VECTOR: models.SparseVector(indices=[7], values=[9.0])}
            )
            for i in ids
        ],
    )
    return ids


def sparse_of(rig, point_id, doc_id="doc-a"):
    vector = rig.points(doc_id, vectors=True)[point_id]["vector"]["sparse"]
    return dict(zip(vector.indices, vector.values, strict=True))


def expected_sparse(payload):
    text = sparse.source_text(
        payload["heading_path"], payload["content_type"], payload["text"], payload["summary"]
    )
    vector = sparse.document_vector(text)
    return dict(zip(vector.indices, vector.values, strict=True))


# ---- the version is recorded, and a change of the tokenizer cannot go unnoticed --------
def test_every_chunk_records_the_tokenizer_version_it_was_made_with(rig):
    rig.ingest(parsed(*sample_elements()))
    payloads = [p["payload"] for p in rig.points().values()]
    assert payloads and all(p["sparse_version"] == sparse.SPARSE_VERSION for p in payloads)
    check = sparse_check(rig.qdrant)
    assert (check.total, check.out_of_date, check.ok) == (3, 0, True)


def fullwidth(text):
    """ASCII letters and digits written as full-width characters (NFKC folds them back)."""
    return "".join(chr(ord(c) + 0xFEE0) if c.isalnum() else c for c in text)


BASIC = (
    "Insured claims: waiting-period premiums apply after 30 days; "
    "hospitalisation expenses & Policy-holders"
)
# every suffix rule of the stemmer, a short word, a word with digits, and "ss" words
STEMMER = (
    "Charges ended; policies denied; classes missed; payments continued; abilities rapidly; "
    "accordingly renewed quickly; stories boxes; filings; hospitalisation; settlements; "
    "cat dogs; abc123s; glass kisses"
)
# accents, the German sharp s, an apostrophe, an underscore, a currency sign, full-width text
UNICODE = (
    "Caf\u00e9 r\u00e9sum\u00e9 na\u00efve " + fullwidth("Fullwidth ABC 123") + " x_y don't "
    "O\u2019Brien\u2019s 100% \u20b95,000 policy-holders Stra\u00dfe"
)
REPEATED = "claims claims claims waiting period period"


def golden_snapshot():
    """Everything the sparse index depends on, in one JSON-able value."""

    def vector(v):
        return [list(v.indices), [round(x, 6) for x in v.values]]

    return {
        "tokens": {name: sparse.tokenize(text) for name, text in
                   {"basic": BASIC, "stemmer": STEMMER, "unicode": UNICODE}.items()},
        "document": {name: vector(sparse.document_vector(text)) for name, text in
                     {"basic": BASIC, "stemmer": STEMMER, "unicode": UNICODE,
                      "repeated": REPEATED}.items()},
        "query": {name: vector(sparse.query_vector(text)) for name, text in
                  {"basic": BASIC, "repeated": REPEATED}.items()},
        "source_text": [
            sparse.source_text(["Part A", "Section 2"], "table", "Row one two", "A short summary"),
            sparse.source_text(["Part A"], "text", "Plain clause text", None),
            sparse.source_text([], "table", "Only text", ""),
            sparse.source_text(["Part A"], "image_caption", "A caption", "ignored summary"),
        ],
    }  # fmt: skip


def golden_digest():
    import hashlib
    import json

    text = json.dumps(golden_snapshot(), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# One digest per tokenizer version. When the tokenizer changes: raise SPARSE_VERSION, ADD a new
# entry with the new digest and KEEP the old ones. Changing an old digest in place defeats the
# test and should be refused in review.
GOLDEN_HISTORY = {1: "9034a307d71f29cbcb7307c10bd110c37b4608c373fe81867942e9292196b888"}


def test_the_tokenizer_output_is_pinned_so_a_change_must_raise_the_version():
    """If this fails you changed how text becomes terms (the word pattern, the stemmer, the hash,
    the weights, the query weights or the text a vector is made from). Raise SPARSE_VERSION in
    app/vector/sparse.py and add the new digest to GOLDEN_HISTORY."""
    assert set(GOLDEN_HISTORY) == set(range(1, sparse.SPARSE_VERSION + 1)), (
        "SPARSE_VERSION and GOLDEN_HISTORY must list the same versions"
    )
    assert golden_digest() == GOLDEN_HISTORY[sparse.SPARSE_VERSION], (
        "the tokenizer's output changed but SPARSE_VERSION did not (or the digest is new)"
    )


def test_the_pin_fails_when_the_output_changes_and_the_version_stays_the_same(monkeypatch):
    """The pin itself is tested: change the stemmer, the hash, a weight or the text a vector is
    made from, and the digest no longer matches the one recorded for this version."""
    assert golden_digest() == GOLDEN_HISTORY[sparse.SPARSE_VERSION]
    monkeypatch.setattr(sparse, "_SUFFIXES", tuple(x for x in sparse._SUFFIXES if x[0] != "s"))
    assert golden_digest() != GOLDEN_HISTORY[sparse.SPARSE_VERSION]  # a stemmer rule
    monkeypatch.undo()
    monkeypatch.setattr(sparse, "token_index", lambda token: hash(token) % (2**32))
    assert golden_digest() != GOLDEN_HISTORY[sparse.SPARSE_VERSION]  # the hash
    monkeypatch.undo()
    real_query = sparse.query_vector
    monkeypatch.setattr(
        sparse,
        "query_vector",
        lambda text: models.SparseVector(
            indices=real_query(text).indices, values=[2.0] * len(real_query(text).indices)
        ),
    )
    assert golden_digest() != GOLDEN_HISTORY[sparse.SPARSE_VERSION]  # the query weights
    monkeypatch.undo()
    real_source = sparse.source_text
    monkeypatch.setattr(
        sparse, "source_text", lambda h, k, t, summary: real_source(h, k, t, summary) + " "
    )
    assert golden_digest() != GOLDEN_HISTORY[sparse.SPARSE_VERSION]  # the composition


def test_the_readable_pins_of_the_tokenizer():
    """The same facts as the digest, written out so that a failure says what moved."""
    assert sparse.tokenize(BASIC) == [
        "insur", "claim", "wait", "period", "premium", "app", "after", "30", "day",
        "hospitalisate", "expens", "policy", "holder",
    ]  # fmt: skip
    assert sparse.tokenize(STEMMER) == [
        "charg", "end", "policy", "deny", "class", "miss", "pay", "continu", "ability",
        "rapid", "accord", "renew", "quick", "story", "box", "fil", "hospitalisate", "settle",
        "cat", "dog", "abc123s", "glass", "kiss",
    ]  # fmt: skip
    assert sparse.tokenize(UNICODE) == [
        "caf\u00e9", "r\u00e9sum\u00e9", "na\u00efve", "fullwidth", "abc", "123", "x", "y",
        "don", "t", "o", "brien", "s", "100", "5", "000", "policy", "holder", "stra\u00dfe",
    ]  # fmt: skip
    document = sparse.document_vector(REPEATED)
    query = sparse.query_vector(REPEATED)
    assert list(document.indices) == list(query.indices) == [230524523, 1776422902, 2954857290]
    assert [round(x, 6) for x in document.values] == [1.693147, 1.0, 2.098612]  # 1 + ln(count)
    assert list(query.values) == [1.0, 1.0, 1.0]  # each distinct term once
    assert (
        sparse.source_text(["Part A", "Section 2"], "table", "Row one two", "A short summary")
        == "Part A Section 2\nA short summary\nRow one two"
    )
    assert sparse.source_text(["Part A"], "text", "Plain clause text", None) == (
        "Part A\nPlain clause text"
    )
    assert sparse.source_text([], "table", "Only text", "") == "\nOnly text"
    assert sparse.source_text(["Part A"], "image_caption", "A caption", "ignored") == (
        "Part A\nA caption"
    )  # only a table uses its summary
    assert sparse.SPARSE_VERSION == 1


def test_the_text_a_sparse_vector_is_made_from_is_the_same_at_ingest_and_at_refresh(rig):
    rig.ingest(parsed(*sample_elements()))
    for payload in (p["payload"] for p in rig.points().values()):
        stored = sparse_of(rig, payload["chunk_id"])
        assert stored == expected_sparse(
            payload
        )  # ingest made it from the chunk; refresh from this


# ---- counting what is out of date --------
def test_status_and_the_check_count_the_chunks_that_are_out_of_date(rig):
    rig.ingest(parsed(*sample_elements()))
    make_stale(rig)
    assert "sparse vectors out of date: 3 of 3" in describe(document_status(rig.ctx, "doc-a"))
    check = sparse_check(rig.qdrant)
    assert (check.total, check.out_of_date, check.ok) == (3, 3, False)


def test_a_chunk_with_no_recorded_version_counts_as_out_of_date(rig):
    rig.ingest(parsed(*sample_elements()))
    some = next(iter(rig.points()))
    rig.qdrant.delete_payload(COLLECTION, keys=["sparse_version"], points=[some])
    assert sparse_check(rig.qdrant).out_of_date == 1


# ---- the refresh: no embedding, one bump, nothing else touched --------
def test_the_refresh_rewrites_only_the_sparse_vectors_with_one_bump_and_no_embedding(rig):
    rig.ingest(parsed(*sample_elements()))
    embedded = len(rig.embedder.calls)
    dense_before = {i: p["vector"]["dense"] for i, p in rig.points(vectors=True).items()}
    ids = make_stale(rig)
    version_before = rig.corpus_version()
    result = refresh_sparse_vectors(rig.ctx)
    assert result.complete and (result.documents, result.chunks_refreshed) == (1, 3)
    assert len(rig.embedder.calls) == embedded  # no embedding call
    assert rig.corpus_version() == version_before + 1  # exactly one bump
    for point_id in ids:
        payload = rig.points()[point_id]["payload"]
        assert payload["sparse_version"] == sparse.SPARSE_VERSION
        assert sparse_of(rig, point_id) == expected_sparse(payload)
    after = rig.points(vectors=True)
    assert {i: p["vector"]["dense"] for i, p in after.items()} == dense_before  # dense untouched
    assert sparse_check(rig.qdrant).ok
    audit = rig.db.query_all("SELECT action, target FROM audit_log WHERE action = 'reindex'")
    assert ("reindex", "all documents") in [tuple(r) for r in audit]


def test_the_refresh_of_several_documents_bumps_the_version_once(rig):
    rig.ingest(parsed(*sample_elements()), doc_id="doc-a")
    rig.ingest(parsed(title("Section 1. Cover"), para("Cover applies to listed events.")),
               doc_id="doc-b")  # fmt: skip
    make_stale(rig, "doc-a")
    make_stale(rig, "doc-b")
    before = rig.corpus_version()
    result = refresh_sparse_vectors(rig.ctx)
    assert result.documents == 2 and result.chunks_refreshed == 4
    assert rig.corpus_version() == before + 1


def test_a_refresh_with_nothing_out_of_date_does_nothing_and_bumps_nothing(rig):
    rig.ingest(parsed(*sample_elements()))
    before = rig.corpus_version()
    result = refresh_sparse_vectors(rig.ctx)
    assert (result.chunks_refreshed, result.documents, result.incomplete) == (0, 0, [])
    assert rig.corpus_version() == before
    assert rig.db.query_all("SELECT 1 FROM audit_log WHERE action = 'reindex'") == []


def test_one_document_can_be_refreshed_on_its_own(rig):
    rig.ingest(parsed(*sample_elements()), doc_id="doc-a")
    rig.ingest(parsed(title("Section 1. Cover"), para("Cover applies.")), doc_id="doc-b")
    make_stale(rig, "doc-a")
    make_stale(rig, "doc-b")
    result = refresh_sparse_vectors(rig.ctx, "doc-a")
    assert (result.documents, result.chunks_refreshed) == (1, 3)
    assert sparse_check(rig.qdrant).out_of_date == 1  # doc-b is still out of date


class Down:
    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise redis.exceptions.ConnectionError("refused")

        return fail

    def pipeline(self, *args, **kwargs):
        raise redis.exceptions.ConnectionError("refused")


def test_a_refresh_refuses_to_start_when_upstash_does_not_accept_staleness_writes(rig):
    rig.ingest(parsed(*sample_elements()))
    make_stale(rig)
    before = {i: sparse_of(rig, i) for i in rig.points()}
    working = rig.cache._client
    rig.cache._client = Down()
    with pytest.raises(pending.CacheUnavailable, match="nothing was changed"):
        refresh_sparse_vectors(rig.ctx)
    assert {i: sparse_of(rig, i) for i in rig.points()} == before  # no vector was written
    assert not pending.is_pending(rig.ctx.marker_dir, "doc-a")
    rig.cache._client = working
    rig.cache._skip_until = 0.0
    assert refresh_sparse_vectors(rig.ctx).complete  # the same command again


def test_a_refresh_whose_bump_failed_keeps_its_marker_and_a_repeat_finishes_it(rig):
    """The residual window: the preflight passed and the bump then failed."""
    rig.ingest(parsed(*sample_elements()))
    make_stale(rig)
    rig.cache.bump_corpus_version = lambda: None
    failed = refresh_sparse_vectors(rig.ctx)
    assert not failed.complete and failed.chunks_refreshed == 3  # the vectors are written
    assert pending.is_pending(rig.ctx.marker_dir, "doc-a")
    assert pending.marker_reason(rig.ctx.marker_dir, "doc-a") == "sparse_refresh"
    del rig.cache.bump_corpus_version
    before = rig.corpus_version()
    repeat = refresh_sparse_vectors(rig.ctx)
    assert repeat.complete and repeat.chunks_refreshed == 0 and repeat.redone == ["doc-a"]
    assert rig.corpus_version() == before + 1  # the one bump, at the end
    assert not pending.is_pending(rig.ctx.marker_dir, "doc-a")
    assert refresh_sparse_vectors(rig.ctx).redone == []  # and now it is a no-op


@pytest.mark.parametrize("reason", ["ingest", "delete", "review"])
def test_a_refresh_leaves_the_marker_of_an_unfinished_run_of_another_kind_alone(rig, reason):
    rig.ingest(parsed(*sample_elements()))
    make_stale(rig)
    pending.mark(rig.ctx.marker_dir, "doc-a", rig.moment, reason)  # an unfinished run waits
    before = rig.corpus_version()
    result = refresh_sparse_vectors(rig.ctx)
    assert result.chunks_refreshed == 3 and result.complete and result.foreign == ["doc-a"]
    assert rig.corpus_version() == before + 1  # the vectors changed, so the version moved
    assert pending.is_pending(rig.ctx.marker_dir, "doc-a")  # but the marker is NOT ours to clear
    assert pending.marker_reason(rig.ctx.marker_dir, "doc-a") == reason


def test_a_chunk_with_no_stored_text_is_left_alone_and_counted(rig):
    rig.ingest(parsed(*sample_elements()))
    ids = make_stale(rig)
    rig.qdrant.set_payload(COLLECTION, payload={"text": ""}, points=[ids[0]])
    result = refresh_sparse_vectors(rig.ctx)
    assert result.chunks_without_text == 1 and result.chunks_refreshed == 2
    assert refreshed_sparse_vector({"text": "  "}, {}) is None


# ---- the ingest path: unchanged chunks are refreshed without re-embedding --------
def test_a_repeat_ingest_rewrites_stale_sparse_vectors_of_unchanged_chunks_without_embedding(rig):
    rig.ingest(parsed(*sample_elements()))
    embedded = len(rig.embedder.calls)
    ids = make_stale(rig)
    before = rig.corpus_version()
    result = rig.ingest(parsed(*sample_elements()))
    assert (result.new, result.changed, result.unchanged) == (0, 0, 3)
    assert result.sparse_refreshed == 3 and result.status == "ok" and result.complete
    assert len(rig.embedder.calls) == embedded  # no embedding call
    assert rig.corpus_version() == before + 1  # one bump
    for point_id in ids:
        assert sparse_of(rig, point_id) == expected_sparse(rig.points()[point_id]["payload"])
    again = rig.ingest(parsed(*sample_elements()))
    assert again.status == "unchanged" and again.sparse_refreshed == 0  # a true no-op once current


def test_a_chunk_whose_stored_heading_differs_gets_its_sparse_vector_made_again(rig):
    """The sparse text includes the heading path. If an unchanged chunk's stored heading path is
    updated (it moved), its sparse vector must follow, or it stays indexed under the old heading."""
    rig.ingest(parsed(*sample_elements()))
    target = next(
        i for i, p in rig.points().items() if "Waiting" in " ".join(p["payload"]["heading_path"])
    )
    # the stored copy remembers another heading (as if the chunk had sat under it before)
    rig.qdrant.set_payload(COLLECTION, payload={"heading_path": ["Old heading"]}, points=[target])
    old_vector = sparse.document_vector(
        "Old heading\nA waiting period of 30 days applies to illness claims."
    )
    rig.qdrant.update_vectors(
        COLLECTION,
        points=[models.PointVectors(id=target, vector={SPARSE_VECTOR: old_vector})],
    )
    stale = sparse_of(rig, target)
    result = rig.ingest(parsed(*sample_elements()))  # the same document: the chunk "moved back"
    assert (result.new, result.changed, result.unchanged) == (0, 0, 3)
    assert result.sparse_refreshed == 1
    payload = rig.points()[target]["payload"]
    assert payload["heading_path"] != ["Old heading"]
    assert sparse_of(rig, target) == expected_sparse(payload) and sparse_of(rig, target) != stale


# ---- the command --------
def run_cli(rig, *argv):
    out, err = [], []
    code = main(
        ["refresh-sparse", *argv],
        env={},
        services_factory=lambda env, needs: Services(rig.ctx),
        out=out.append,
        err=err.append,
    )
    return code, out, err


def test_the_refresh_command_reports_counts_only(rig):
    rig.ingest(parsed(*sample_elements()))
    make_stale(rig)
    code, out, err = run_cli(rig)
    assert code == 0 and err == []
    assert out[0].startswith("sparse vectors refreshed: 3 chunk(s) in 1 document(s)")
    assert "no embedding call was made" in out[0]
    code, out, _ = run_cli(rig)
    assert code == 0 and any("nothing was out of date" in line for line in out)


def test_the_refresh_command_can_name_one_document(rig):
    rig.ingest(parsed(*sample_elements()))
    make_stale(rig)
    code, out, _ = run_cli(rig, "--doc-id", "doc-a")
    assert code == 0 and "3 chunk(s) in 1 document(s)" in out[0]


def test_a_refresh_that_cannot_start_exits_with_1_and_says_nothing_was_changed(rig):
    rig.ingest(parsed(*sample_elements()))
    make_stale(rig)
    rig.cache._client = Down()
    code, out, err = run_cli(rig)
    assert code == 1 and any("cache unavailable, nothing was changed" in line for line in err)


def test_an_incomplete_refresh_exits_with_1_and_says_to_run_it_again(rig):
    rig.ingest(parsed(*sample_elements()))
    make_stale(rig)
    rig.cache.bump_corpus_version = lambda: None
    code, out, err = run_cli(rig)
    assert code == 1 and any("refresh incomplete" in line for line in err)


def test_the_command_names_documents_whose_unfinished_run_was_left_alone(rig):
    rig.ingest(parsed(*sample_elements()))
    make_stale(rig)
    pending.mark(rig.ctx.marker_dir, "doc-a", rig.moment, "delete")
    code, out, _ = run_cli(rig)
    assert code == 0 and any("unfinished ingest, delete or review" in line for line in out)


# ---- the start-up check --------
def test_the_start_up_check_warns_with_counts_when_any_chunk_differs(tmp_path):
    pipeline_rig = PipelineRig(tmp_path)
    try:
        pipeline = pipeline_rig.pipeline
        ok = pipeline.check_index()
        assert ok.ok and "sparse_version_mismatch" not in pipeline_rig.event_names
        ids = list(pipeline_rig.rig.points("doc-a"))
        pipeline_rig.rig.qdrant.set_payload(
            COLLECTION, payload={"sparse_version": 0}, points=ids[:1]
        )
        check = pipeline.check_index()
        assert (check.total, check.out_of_date) == (len(ids), 1)
        assert ("sparse_version_mismatch", {"out_of_date": 1, "total": len(ids)}) in (
            pipeline_rig.events
        )
        assert isinstance(pipeline, Pipeline)
    finally:
        pipeline_rig.close()


def test_the_start_up_check_reports_unavailable_instead_of_raising(tmp_path):
    pipeline_rig = PipelineRig(tmp_path)
    try:

        class Broken:
            def count(self, *args, **kwargs):
                raise RuntimeError("down with some text")

        pipeline_rig.deps.qdrant = Broken()
        assert pipeline_rig.pipeline.check_index() is None
        assert (
            "sparse_check_unavailable",
            {"reason": "StoreUnavailable"},
        ) in pipeline_rig.events or ("sparse_check_unavailable" in pipeline_rig.event_names)
    finally:
        pipeline_rig.close()
