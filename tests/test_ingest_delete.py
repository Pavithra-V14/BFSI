"""Deleting a document (spec 6.7, 8.5, 10.3), on in-memory Qdrant, fakeredis and SQLite."""

import json
import uuid

import pytest

from app.cache.store import ChunkRef
from app.ingest.delete import delete_document, flag_source_removed
from app.ingest.lock import DocumentBusy, document_lock
from tests.ingest_fixtures import Rig, parsed, sample_elements


@pytest.fixture
def rig(tmp_path):
    made = Rig(tmp_path)
    yield made
    made.close()


def add_chat(rig, citations):
    user_id, session_id, message_id = (str(uuid.uuid4()) for _ in range(3))
    rig.db.execute(
        "INSERT INTO users (id, username, email, password_hash) VALUES (?, ?, ?, ?)",
        (user_id, f"u{user_id[:8]}", f"{user_id[:8]}@example.test", "h"),
    )
    rig.db.execute("INSERT INTO sessions (id, user_id) VALUES (?, ?)", (session_id, user_id))
    rig.db.execute(
        "INSERT INTO messages (id, session_id, seq, role, content, citations) "
        "VALUES (?, ?, 1, 'assistant', 'masked', ?)",
        (message_id, session_id, json.dumps(citations)),
    )
    return message_id


def flagged(rig, message_id):
    return bool(
        rig.db.query_one("SELECT source_removed FROM messages WHERE id = ?", (message_id,))[0]
    )


def add_allowlist(rig, doc_id, text="IRDAI"):
    rig.db.execute(
        "INSERT INTO allowlist_entities (id, doc_id, text, entity_type, status, access_role) "
        "VALUES (?, ?, ?, 'regulator', 'approved', 'customer')",
        (str(uuid.uuid4()), doc_id, text),
    )


def test_delete_removes_every_vector_at_once(rig):
    rig.ingest(parsed(*sample_elements()))
    rig.ingest(parsed(*sample_elements()), doc_id="doc-b")
    result = delete_document(rig.ctx, "doc-a")
    assert result.chunks == 3 and result.complete
    assert rig.points("doc-a") == {}  # gone, not merely inactive
    assert len(rig.points("doc-b")) == 3  # another document is untouched


def test_delete_bumps_the_version_and_clears_entries_that_cited_the_document(rig):
    rig.ingest(parsed(*sample_elements()))
    point_id, point = next(iter(rig.points().items()))
    ref = ChunkRef(point_id, point["payload"]["content_hash"], "doc-a")
    rig.cache.put_entry("entry", {"a": 1}, [ref], corpus_version=rig.corpus_version())
    version = rig.corpus_version()
    delete_document(rig.ctx, "doc-a")
    assert rig.corpus_version() == version + 1 and rig.redis.exists("entry") == 0


def test_delete_removes_the_documents_allow_list_entries_and_only_those(rig):
    rig.ingest(parsed(*sample_elements()))
    add_allowlist(rig, "doc-a")
    add_allowlist(rig, "doc-a", "SEBI")
    add_allowlist(rig, "doc-b")
    result = delete_document(rig.ctx, "doc-a")
    assert result.allowlist_entries == 2
    rows = rig.db.query_all("SELECT doc_id FROM allowlist_entities")
    assert rows == [("doc-b",)]


def test_delete_flags_transcripts_that_cited_the_document(rig):
    cited = add_chat(rig, [{"doc_id": "doc-a", "clause_ref": "2"}])
    other = add_chat(rig, [{"doc_id": "doc-b"}])
    none = add_chat(rig, [])
    lookalike = add_chat(rig, [{"doc_id": "x", "note": "doc-a"}])
    rig.ingest(parsed(*sample_elements()))
    result = delete_document(rig.ctx, "doc-a")
    assert result.transcripts_flagged == 1
    assert flagged(rig, cited)
    assert not flagged(rig, other) and not flagged(rig, none) and not flagged(rig, lookalike)


def test_delete_writes_one_audit_row_with_a_count_and_no_text(rig):
    rig.ingest(parsed(*sample_elements()))
    delete_document(rig.ctx, "doc-a")
    rows = rig.db.query_all(
        "SELECT actor, action, target, details FROM audit_log WHERE action = 'delete'"
    )
    assert [(a, act, t) for a, act, t, _ in rows] == [("cli", "delete", "doc-a")]
    details = json.loads(rows[0][3])
    assert details["chunks"] == 3 and "waiting" not in rows[0][3]


def test_deleting_twice_is_safe_and_writes_no_second_audit_row(rig):
    rig.ingest(parsed(*sample_elements()))
    delete_document(rig.ctx, "doc-a")
    second = delete_document(rig.ctx, "doc-a")
    assert second.chunks == 0 and second.complete
    count = rig.db.query_one("SELECT COUNT(*) FROM audit_log WHERE action = 'delete'")[0]
    assert count == 1


def test_deleting_a_document_that_never_existed_is_a_quiet_no_op(rig):
    result = delete_document(rig.ctx, "never-there")
    assert (result.chunks, result.allowlist_entries, result.transcripts_flagged) == (0, 0, 0)
    assert rig.db.query_one("SELECT COUNT(*) FROM audit_log")[0] == 0


def test_a_failed_version_bump_makes_the_delete_incomplete_and_a_rerun_finishes_it(rig):
    rig.ingest(parsed(*sample_elements()))
    good_bump = rig.cache.bump_corpus_version
    rig.cache.bump_corpus_version = lambda: None
    result = delete_document(rig.ctx, "doc-a")
    assert not result.complete and rig.points() == {}  # the vectors are gone, the run says so
    rig.cache.bump_corpus_version = good_bump
    version = rig.corpus_version()
    assert delete_document(rig.ctx, "doc-a").complete
    assert rig.corpus_version() == version + 1  # the rerun did the bump that was missed


def test_a_failed_invalidation_makes_the_delete_incomplete(rig):
    rig.ingest(parsed(*sample_elements()))
    rig.cache.invalidate_doc = lambda doc_id: None
    result = delete_document(rig.ctx, "doc-a")
    assert not result.complete and "document cache invalidation failed" in result.incomplete[0]


def test_delete_is_refused_while_the_document_is_being_ingested(rig):
    rig.ingest(parsed(*sample_elements()))
    with document_lock(rig.ctx.lock_dir, "doc-a"):
        with pytest.raises(DocumentBusy):
            delete_document(rig.ctx, "doc-a")
    assert len(rig.points()) == 3


def test_the_flag_rule_reads_the_doc_id_of_each_citation(rig):
    mine = add_chat(rig, [{"doc_id": "doc-a"}, {"doc_id": "doc-b"}])
    broken = add_chat(rig, [])
    rig.db.execute("UPDATE messages SET citations = ? WHERE id = ?", ('not json "doc-a"', broken))
    assert flag_source_removed(rig.db, "doc-a") == 1 and flagged(rig, mine)
    assert not flagged(rig, broken)


def test_an_invalid_doc_id_is_refused(rig):
    from app.ingest.model import IngestError

    with pytest.raises(IngestError):
        delete_document(rig.ctx, "../etc")
