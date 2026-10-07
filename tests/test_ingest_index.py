"""Indexing and incremental reindex (spec 6.3, 6.4, 10.3), on in-memory Qdrant and fakeredis."""

import pytest

from app.cache.store import ChunkRef
from app.gateway.embeddings import DIMENSIONS
from app.ingest.index import IngestContext, ingest_document
from app.ingest.lock import DocumentBusy, document_lock
from app.ingest.model import IngestError
from app.store_errors import VectorStoreUnavailable
from app.vector.documents import chunk_is_active
from tests.ingest_fixtures import (
    Rig,
    filler,
    para,
    parsed,
    sample_elements,
    table,
    title,
)


@pytest.fixture
def rig(tmp_path):
    made = Rig(tmp_path)
    yield made
    made.close()


def by_ref(points):
    return {p["payload"]["clause_ref"]: (pid, p) for pid, p in points.items()}


def cite(rig, key, *chunks):
    """Put a cache entry that cites the given (chunk_id, content_hash) pairs."""
    refs = [ChunkRef(chunk_id=i, content_hash=h, doc_id="doc-a") for i, h in chunks]
    assert rig.cache.put_entry(key, {"answer": "x"}, refs, corpus_version=rig.corpus_version())


def alive(rig, key):
    return rig.redis.exists(key) == 1


# ---- first ingest -----------------------------------------------------------------------------
def test_a_first_ingest_writes_every_chunk_with_the_payload_fields_of_6_4(rig):
    result = rig.ingest(parsed(*sample_elements()))
    assert (result.status, result.new, result.doc_version) == ("ok", 3, 1) and result.complete
    points = rig.points(vectors=True)
    assert len(points) == 3
    expected = {
        "doc_id", "source", "chunk_id", "clause_ref", "part_index", "content_hash", "doc_version",
        "chunk_version", "effective_date", "effective_to", "page_start", "page_end", "heading_path",
        "clause_type", "product_line", "content_type", "image_ref", "access_role", "ingested_at",
        "parser", "text", "summary", "quarantine", "quarantine_reason",
    }  # fmt: skip
    for point_id, point in points.items():
        payload = point["payload"]
        assert set(payload) == expected
        assert payload["chunk_id"] == point_id
        assert payload["effective_to"] is None and chunk_is_active(payload)
        assert (payload["doc_version"], payload["chunk_version"]) == (1, 1)
        assert payload["access_role"] == "customer" and payload["product_line"] == "insurance"
        assert payload["content_type"] == "text" and payload["parser"] == "unstructured"
        assert len(point["vector"]["dense"]) == DIMENSIONS
        assert point["vector"]["sparse"].indices  # the sparse vector is stored too
    assert len(rig.active()) == 3


def test_the_chunk_text_is_stored_so_retrieval_can_return_it(rig):
    rig.ingest(parsed(*sample_elements()))
    texts = {p["payload"]["clause_ref"]: p["payload"]["text"] for p in rig.points().values()}
    assert "waiting period of 30 days" in texts["2"]


def test_embedding_uses_the_document_title_and_none_when_there_is_none(rig):
    rig.ingest(parsed(*sample_elements()), title="Policy A")
    assert rig.embedder.titles == ["Policy A"]
    rig.ingest(parsed(*sample_elements(), para("3.1 A new rule appears.")), title=None)
    assert rig.embedder.titles[-1] is None  # the embedding client turns None into "none"


def test_a_first_ingest_bumps_the_corpus_version_and_writes_one_audit_row(rig):
    before = rig.corpus_version()
    rig.ingest(parsed(*sample_elements()))
    assert rig.corpus_version() == before + 1
    rows = rig.db.query_all("SELECT actor, action, target, details FROM audit_log")
    assert [(a, act, t) for a, act, t, _ in rows] == [("cli", "ingest", "doc-a")]
    assert "waiting period" not in rows[0][3]  # no document text in the audit row


# ---- re-ingest: nothing changed ------------------------------------------------------------------
def test_ingesting_the_same_document_again_changes_nothing_and_embeds_nothing(rig):
    rig.ingest(parsed(*sample_elements()))
    snapshot = rig.points(vectors=True)
    version = rig.corpus_version()
    calls = len(rig.embedder.calls)
    result = rig.ingest(parsed(*sample_elements()))
    assert (result.status, result.new, result.changed, result.removed) == ("unchanged", 0, 0, 0)
    assert result.unchanged == 3 and result.complete
    assert len(rig.embedder.calls) == calls  # no embedding call at all
    assert rig.corpus_version() == version  # no bump
    assert rig.points(vectors=True) == snapshot  # not even a payload field moved


def test_a_whitespace_only_change_is_not_a_change(rig):
    rig.ingest(parsed(*sample_elements()))
    version, calls = rig.corpus_version(), len(rig.embedder.calls)
    elements = sample_elements()
    elements[3] = para("  A waiting period   of 30 days\n applies to illness claims.  ")
    result = rig.ingest(parsed(*elements))
    assert result.status == "unchanged" and result.changed == 0
    assert len(rig.embedder.calls) == calls and rig.corpus_version() == version


# ---- re-ingest: edits, inserts, removals -----------------------------------------------------
def test_inserting_a_clause_in_the_middle_leaves_every_other_chunk_untouched(rig):
    rig.ingest(parsed(*sample_elements()))
    before = by_ref(rig.points(vectors=True))
    elements = sample_elements()
    elements[2:2] = [title("Section 1A. Inserted"), para("A brand new clause is added here.")]
    result = rig.ingest(parsed(*elements))
    assert (result.new, result.changed, result.removed, result.unchanged) == (1, 0, 0, 3)
    after = by_ref(rig.points(vectors=True))
    for ref in ("1", "2", "3"):
        old_id, old = before[ref]
        new_id, new = after[ref]
        assert new_id == old_id
        assert new["payload"]["chunk_version"] == old["payload"]["chunk_version"] == 1
        assert new["payload"]["doc_version"] == old["payload"]["doc_version"] == 1
        assert new["vector"]["dense"] == old["vector"]["dense"]  # same embedding
    assert after["1A"][1]["payload"]["doc_version"] == 2
    assert len(rig.embedder.calls[-1]) == 1  # only the new clause was embedded


def test_an_edited_clause_is_re_embedded_with_the_same_id_and_a_new_version(rig):
    rig.ingest(parsed(*sample_elements()))
    old_id, old = by_ref(rig.points(vectors=True))["2"]
    elements = sample_elements()
    elements[3] = para("A waiting period of 60 days applies to illness claims.")
    result = rig.ingest(parsed(*elements))
    assert (result.changed, result.new, result.removed, result.unchanged) == (1, 0, 0, 2)
    new_id, new = by_ref(rig.points(vectors=True))["2"]
    assert new_id == old_id and new["payload"]["chunk_version"] == 2
    assert new["payload"]["doc_version"] == 2 and "60 days" in new["payload"]["text"]
    assert new["vector"]["dense"] != old["vector"]["dense"]
    assert len(rig.embedder.calls[-1]) == 1  # only the changed clause
    assert chunk_is_active(new["payload"])


def test_a_removed_clause_stops_being_returned_at_once(rig):
    rig.ingest(parsed(*sample_elements()))
    gone_id = by_ref(rig.points())["2"][0]
    elements = sample_elements()
    del elements[2:4]
    result = rig.ingest(parsed(*elements))
    assert (result.removed, result.new, result.changed) == (1, 0, 0)
    assert gone_id not in rig.active()  # not returned by the active-only filter
    stored = rig.points()[gone_id]["payload"]
    assert stored["effective_to"] and not chunk_is_active(stored)  # kept, but inactive
    assert len(rig.active()) == 2


def test_a_reordered_document_changes_nothing(rig):
    rig.ingest(parsed(*sample_elements()))
    before = rig.points(vectors=True)
    elements = sample_elements()
    reordered = elements[4:6] + elements[0:2] + elements[2:4]  # clause 3 first, then 1, then 2
    result = rig.ingest(parsed(*reordered))
    assert (result.status, result.unchanged) == ("unchanged", 3)
    assert rig.points(vectors=True) == before and len(rig.embedder.calls) == 1


def test_content_found_under_another_clause_number_keeps_the_stored_id_and_vector():
    from app.ingest.identity import assign_identities
    from app.ingest.index import Existing, plan_changes
    from app.ingest.model import Chunk

    chunk = Chunk("text", "Same words here.", "Same words here.", "9", ("Section 9",), 1, 1)
    [ident] = assign_identities("doc-a", [chunk])
    stored = Existing(
        "stored-id",
        {"content_hash": ident.content_hash, "content_type": "text", "effective_to": None},
    )
    plan = plan_changes("doc-a", [ident], [stored])
    assert [(i.chunk_id, e.id) for i, e in plan.unchanged] == [("stored-id", "stored-id")]
    assert not plan.new and not plan.changed and not plan.removed


def test_a_new_chunk_whose_id_is_taken_by_an_adopted_chunk_gets_a_fresh_id():
    from app.ingest.identity import assign_identities, point_id
    from app.ingest.index import Existing, plan_changes
    from app.ingest.model import Chunk

    moved = Chunk("text", "Old words.", "Old words.", "3", (), 1, 1)
    fresh = Chunk("text", "Brand new words.", "Brand new words.", "4", (), 1, 1)
    one, two = assign_identities("doc-a", [moved, fresh])
    taken = point_id("doc-a", "4", 0)  # the id the fresh chunk would get
    stored = Existing(taken, {"content_hash": one.content_hash, "content_type": "text"})
    plan = plan_changes("doc-a", [one, two], [stored])
    assert plan.unchanged[0][0].chunk_id == taken
    assert plan.new[0].chunk_id not in (taken, "") and plan.new[0].chunk_id != taken


def _on_page(element, page):
    from dataclasses import replace

    return replace(element, page=page)


def test_a_page_change_refreshes_the_payload_without_re_embedding_or_bumping(rig):
    rig.ingest(parsed(*sample_elements()))
    version, calls = rig.corpus_version(), len(rig.embedder.calls)
    moved = [_on_page(e, 2) for e in sample_elements()]
    result = rig.ingest(parsed(*moved))
    assert result.status == "unchanged" and result.unchanged == 3
    assert {p["payload"]["page_start"] for p in rig.points().values()} == {2}
    assert len(rig.embedder.calls) == calls and rig.corpus_version() == version


def test_changing_access_role_refreshes_every_chunk_and_clears_the_documents_cache(rig):
    rig.ingest(parsed(*sample_elements()))
    point_id, point = next(iter(rig.points().items()))
    cite(rig, "entry-1", (point_id, point["payload"]["content_hash"]))
    version = rig.corpus_version()
    result = rig.ingest(parsed(*sample_elements()), access_role="staff")
    assert result.status == "ok" and result.unchanged == 3
    assert {p["payload"]["access_role"] for p in rig.points().values()} == {"staff"}
    assert rig.corpus_version() == version + 1 and not alive(rig, "entry-1")


# ---- cache invalidation (10.3) ------------------------------------------------------------------
def test_an_edit_clears_entries_that_cited_the_chunk_and_keeps_the_rest(rig):
    rig.ingest(parsed(*sample_elements()))
    refs = by_ref(rig.points())
    cite(rig, "cites-2", (refs["2"][0], refs["2"][1]["payload"]["content_hash"]))
    cite(rig, "cites-3", (refs["3"][0], refs["3"][1]["payload"]["content_hash"]))
    elements = sample_elements()
    elements[3] = para("A waiting period of 60 days applies to illness claims.")
    rig.ingest(parsed(*elements))
    assert not alive(rig, "cites-2") and alive(rig, "cites-3")  # an unchanged chunk keeps entries


def test_a_new_chunk_clears_the_entries_that_cited_the_same_document(rig):
    rig.ingest(parsed(*sample_elements()))
    refs = by_ref(rig.points())
    cite(rig, "cites-3", (refs["3"][0], refs["3"][1]["payload"]["content_hash"]))
    elements = sample_elements() + [title("Section 4. New"), para("A fresh clause is added.")]
    rig.ingest(parsed(*elements))
    assert not alive(rig, "cites-3")


def test_a_removed_chunk_clears_the_entries_that_cited_it(rig):
    rig.ingest(parsed(*sample_elements()))
    refs = by_ref(rig.points())
    cite(rig, "cites-3", (refs["3"][0], refs["3"][1]["payload"]["content_hash"]))
    rig.ingest(parsed(*sample_elements()[:4]))
    assert not alive(rig, "cites-3")


def test_nothing_is_cleared_when_nothing_changed(rig):
    rig.ingest(parsed(*sample_elements()))
    refs = by_ref(rig.points())
    cite(rig, "kept", (refs["1"][0], refs["1"][1]["payload"]["content_hash"]))
    rig.ingest(parsed(*sample_elements()))
    assert alive(rig, "kept")


def test_the_version_is_bumped_after_the_writes_and_the_invalidation_after_the_bump(rig):
    order = []
    original_upsert = rig.qdrant.upsert
    rig.qdrant.upsert = lambda *a, **k: (order.append("write"), original_upsert(*a, **k))[1]
    original_bump = rig.cache.bump_corpus_version
    rig.cache.bump_corpus_version = lambda: (order.append("bump"), original_bump())[1]
    original_doc = rig.cache.invalidate_doc
    rig.cache.invalidate_doc = lambda d: (order.append("invalidate"), original_doc(d))[1]
    rig.ingest(parsed(*sample_elements()))
    assert order == ["write", "bump", "invalidate"]


def test_a_failed_version_bump_makes_the_ingest_incomplete(rig):
    rig.cache.bump_corpus_version = lambda: None
    result = rig.ingest(parsed(*sample_elements()))
    assert not result.complete and "corpus version was not bumped" in result.incomplete[0]
    assert len(rig.active()) == 3  # the chunks are written; the run says it is not finished


def test_a_failed_invalidation_makes_the_ingest_incomplete(rig):
    rig.ingest(parsed(*sample_elements()))
    rig.cache.invalidate_chunk = lambda chunk_id: None
    elements = sample_elements()
    elements[3] = para("A waiting period of 60 days applies to illness claims.")
    result = rig.ingest(parsed(*elements))
    assert not result.complete and any("invalidation" in m for m in result.incomplete)
    rig.cache.invalidate_doc = lambda doc_id: None
    result = rig.ingest(parsed(*elements, title("Section 4. New"), para("More rules apply.")))
    assert any("document cache invalidation" in m for m in result.incomplete)


# ---- nothing half-indexed ------------------------------------------------------------------------
def test_a_failed_page_means_nothing_is_written(rig):
    result = rig.ingest(parsed(*sample_elements(), failed=(2,)))
    assert result.status == "failed_pages" and [f.page for f in result.failed_pages] == [2]
    assert rig.points() == {} and rig.embedder.calls == []
    assert rig.db.query_all("SELECT * FROM audit_log") == []


def test_on_re_ingest_the_old_version_stays_active_until_every_page_succeeds(rig):
    rig.ingest(parsed(*sample_elements()))
    before = rig.points(vectors=True)
    version = rig.corpus_version()
    edited = sample_elements()
    edited[3] = para("A waiting period of 90 days applies to illness claims.")
    result = rig.ingest(parsed(*edited, failed=(1,)))
    assert result.status == "failed_pages"
    assert rig.points(vectors=True) == before and rig.corpus_version() == version
    assert rig.ingest(parsed(*edited)).changed == 1  # the retry then succeeds


def test_a_failed_embedding_leaves_the_index_untouched(rig):
    rig.ingest(parsed(*sample_elements()))
    before = rig.points(vectors=True)
    rig.embedder.fail = True
    elements = sample_elements()
    elements[3] = para("A waiting period of 60 days applies to illness claims.")
    with pytest.raises(RuntimeError):
        rig.ingest(parsed(*elements))
    assert rig.points(vectors=True) == before


def test_bad_vectors_are_refused_before_anything_is_written(rig):
    rig.ctx.embed = lambda texts, title: [[0.0] * DIMENSIONS for _ in texts]  # zero vectors
    with pytest.raises(IngestError, match="unusable vectors"):
        rig.ingest(parsed(*sample_elements()))
    assert rig.points() == {}


def test_an_empty_parse_never_deactivates_a_stored_document(rig):
    rig.ingest(parsed(*sample_elements()))
    with pytest.raises(IngestError, match="no text"):
        rig.ingest(parsed())
    assert len(rig.active()) == 3


def test_an_unreachable_qdrant_is_reported_not_swallowed(rig, monkeypatch):
    monkeypatch.setattr(rig.qdrant, "upsert", _raise_connection)
    with pytest.raises(VectorStoreUnavailable):
        rig.ingest(parsed(*sample_elements()))


def _raise_connection(*args, **kwargs):
    raise ConnectionError("down")


# ---- tables, ids, locks, validation -----------------------------------------------------------
def test_a_table_chunk_is_indexed_as_a_table_and_its_summary_is_not_hashed(rig):
    elements = [
        title("Section 4. Limits"),
        para("Limits are listed below."),
        table([["a", "b"], ["1", "2"]]),
    ]
    rig.ingest(parsed(*elements))
    kinds = sorted(p["payload"]["content_type"] for p in rig.points().values())
    assert kinds == ["table", "text"]
    ids = {p["payload"]["content_type"]: pid for pid, p in rig.points().items()}
    assert ids["table"] != ids["text"]  # same clause number, different identity


def test_ids_are_unique_and_stable_across_documents(rig):
    rig.ingest(parsed(*sample_elements()))
    rig.ingest(parsed(*sample_elements()), doc_id="doc-b")
    a, b = set(rig.points("doc-a")), set(rig.points("doc-b"))
    assert a and b and not a & b


def test_the_per_document_lock_refuses_a_second_run_on_the_same_document(rig):
    with document_lock(rig.ctx.lock_dir, "doc-a"):
        with pytest.raises(DocumentBusy):
            rig.ingest(parsed(*sample_elements()))
        rig.ingest(parsed(*sample_elements()), doc_id="doc-b")  # another document is fine
    assert rig.ingest(parsed(*sample_elements())).status == "ok"  # released afterwards


@pytest.mark.parametrize("doc_id", ["", "Doc A", "../x", "a/b", "A", "-a", "a" * 70])
def test_a_bad_doc_id_is_refused(rig, doc_id):
    with pytest.raises(IngestError, match="doc_id"):
        rig.ingest(parsed(*sample_elements()), doc_id=doc_id)


def test_bad_doc_type_and_role_are_refused(rig):
    with pytest.raises(IngestError, match="doc_type"):
        rig.ingest(parsed(*sample_elements()), doc_type="other")
    with pytest.raises(IngestError, match="access_role"):
        rig.ingest(parsed(*sample_elements()), access_role="everyone")


def test_a_second_ingest_after_a_first_is_audited_as_a_reindex(rig):
    rig.ingest(parsed(*sample_elements()))
    rig.ingest(parsed(*sample_elements(), para("3.1 A new rule appears.")))
    actions = [r[0] for r in rig.db.query_all("SELECT action FROM audit_log ORDER BY created_at")]
    assert actions == ["ingest", "reindex"]


def test_a_large_clause_is_stored_as_several_parts_with_stable_ids(rig):
    elements = [title("Section 5. Long"), para(filler(1500))]
    rig.ingest(parsed(*elements))
    parts = sorted(p["payload"]["part_index"] for p in rig.points().values())
    assert parts == list(range(len(parts))) and len(parts) >= 3
    ids = set(rig.points())
    assert rig.ingest(parsed(*elements)).status == "unchanged" and set(rig.points()) == ids


def test_ingest_context_is_a_plain_dataclass_of_services(rig):
    assert isinstance(rig.ctx, IngestContext) and callable(ingest_document)
