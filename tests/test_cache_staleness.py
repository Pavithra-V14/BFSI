"""The staleness matrix of spec 10.3, end to end (Phase 9): the real ingest, the real delete and the
real pipeline over fakeredis and in-memory Qdrant. Each test names the rule it proves."""

import asyncio

import pytest
import redis

from app.ingest import pending
from app.ingest.delete import delete_document
from app.ingest.pending import CacheUnavailable
from app.ingest.review import review_chunks
from app.pipeline.roles import VISIBLE_ROLES, role_map_version
from tests.ingest_fixtures import para, parsed, title
from tests.pipeline_rig import PipelineRig
from tests.test_ingest_enrich import Tools, enricher_for
from tests.test_ingest_round2 import poisoned

QUESTION = "What is the waiting period for illness claims?"


def run(coro):
    return asyncio.run(coro)


def document(waiting="30 days", extra=False, moved=False, spaced=False):
    waiting_text = f"A waiting period of {waiting} applies to illness claims."
    if spaced:
        waiting_text = waiting_text.replace("waiting period", "waiting   period")
    sections = [
        (
            title("Section 1. Definitions"),
            para("Policy means this contract. Insured means the person covered."),
        ),
        (title("Section 2. Waiting periods"), para(waiting_text)),
        (
            title("Section 3. Exclusions"),
            para("War and nuclear risks are excluded. Self harm is excluded."),
        ),
    ]
    if moved:
        sections[1], sections[2] = sections[2], sections[1]
    items = [item for pair in sections for item in pair]
    if extra:
        items += [
            title("Section 4. Renewals"),
            para("The policy renews every year on the same date."),
        ]
    return parsed(*items)


@pytest.fixture
def rig(tmp_path):
    made = PipelineRig(tmp_path, documents=[("doc-a", "customer", document())])
    yield made
    made.close()


def entries(rig):
    return rig.rig.redis.keys("ans:*")


def cached_once(rig, user):
    first = run(rig.ask(QUESTION, user=user))
    assert first.kind == "answer" and not first.cached
    keys = entries(rig)
    assert len(keys) == 1, "the answer was cached: the test proves something"
    return keys[0]


# ---- 10.3 items 1 to 3: the version, the reverse sets and the edit / new / move / whitespace rules
def test_an_edited_chunk_invalidates_the_entries_that_cited_it(rig):
    old_key = cached_once(rig, rig.new_user())
    before = rig.rig.cache.corpus_version()
    result = rig.rig.ingest(document(waiting="45 days"))
    assert result.changed == 1 and result.complete
    assert not rig.rig.redis.exists(old_key)  # removed by the invalidation, not only unreachable
    assert rig.rig.cache.corpus_version() > before  # and the version moved on


def test_an_unchanged_chunk_that_only_moved_keeps_its_entries(rig):
    old_key = cached_once(rig, rig.new_user())
    before = rig.rig.cache.corpus_version()
    result = rig.rig.ingest(document(moved=True))
    assert (result.new, result.changed, result.removed) == (0, 0, 0)
    assert rig.rig.redis.exists(old_key)  # kept: nothing it cited changed
    assert rig.rig.cache.corpus_version() == before


def test_a_new_chunk_invalidates_the_entries_that_cited_the_same_document(rig):
    old_key = cached_once(rig, rig.new_user())
    result = rig.rig.ingest(document(extra=True))
    assert result.new == 1 and result.complete
    assert not rig.rig.redis.exists(old_key)


def test_a_whitespace_only_change_changes_nothing(rig):
    old_key = cached_once(rig, rig.new_user())
    before = rig.rig.cache.corpus_version()
    result = rig.rig.ingest(document(spaced=True))
    assert result.status == "unchanged"
    assert rig.rig.redis.exists(old_key) and rig.rig.cache.corpus_version() == before


def test_a_change_to_who_may_see_a_document_invalidates_its_entries(rig):
    user = rig.new_user()
    old_key = cached_once(rig, user)
    rig.rig.ingest(document(), access_role="staff")  # the same chunks, now staff only
    assert not rig.rig.redis.exists(old_key)
    refused = run(rig.ask(QUESTION, user=user))  # a customer no longer gets it from anywhere
    assert refused.kind == "refusal" and not refused.cached and refused.citations == []


def test_a_delete_removes_the_entries_and_the_document_is_no_longer_answered(rig):
    user = rig.new_user()
    old_key = cached_once(rig, user)
    deleted = delete_document(rig.rig.ctx, "doc-a")
    assert deleted.complete and deleted.chunks == 3
    assert not rig.rig.redis.exists(old_key)
    again = run(rig.ask(QUESTION, user=user))
    assert again.kind == "refusal" and not again.cached


# ---- 10.3 item 5: a request that straddles an ingest --------
def test_a_request_that_straddles_an_ingest_does_not_write_to_the_cache(rig):
    """A REAL interleaving: the request has done its lookup and is generating when an ingest runs
    to completion (new chunk, real version bump). When the request reaches the cache write, the
    version it started with is gone, so it must not write."""
    rig.gateway.delay["answer"] = 2.0
    user = rig.new_user()
    seen = {}

    async def scenario():
        task = asyncio.create_task(rig.ask(QUESTION, user=user))
        for _ in range(400):  # until the request is inside the answer model call
            await asyncio.sleep(0.02)
            if rig.gateway.calls_of("complete", what="answer"):
                break
        assert not task.done(), "the request is still in flight"
        seen["before"] = rig.rig.cache.corpus_version()
        seen["ingest"] = await asyncio.to_thread(rig.rig.ingest, document(extra=True))
        seen["after"] = rig.rig.cache.corpus_version()
        assert not task.done(), "the ingest finished while the request was still in flight"
        return await task

    result = run(scenario())
    assert seen["ingest"].new == 1 and seen["after"] > seen["before"]
    assert result.kind == "answer"  # the user still gets the answer
    assert ("cache_write_skipped", {"reason": "corpus_changed"}) in rig.events
    assert entries(rig) == []  # and nothing stale was left behind


# ---- 10.3 item 4: the read-time check (Phase 8) discards what changed under an entry ----
def test_an_entry_whose_chunk_was_edited_behind_the_cache_is_discarded_on_read(rig):
    user = rig.new_user()
    cached_once(rig, user)
    # edit the stored chunk WITHOUT going through ingest (no bump, no invalidation)
    for point_id in rig.rig.points("doc-a"):
        rig.rig.qdrant.set_payload(
            "documents", payload={"content_hash": "edited-behind-the-back"}, points=[point_id]
        )
    again = run(rig.ask(QUESTION, user=user))
    assert not again.cached and "cache_entry_discarded" in rig.event_names


def test_an_answer_that_cites_a_chunk_without_a_content_hash_is_not_cached(rig):
    """An entry that verifies fewer chunks than the answer cited could go stale unseen."""
    for point_id in rig.rig.points("doc-a"):
        rig.rig.qdrant.set_payload("documents", payload={"content_hash": None}, points=[point_id])
    result = run(rig.ask(QUESTION, user=rig.new_user()))
    assert result.kind == "answer"
    assert entries(rig) == []
    skipped = [f for n, f in rig.events if n == "cache_write_skipped"]
    assert skipped and skipped[0]["reason"] == "chunk_without_hash" and skipped[0]["chunks"] >= 1


# ---- the version is never lost to zero --------
def test_a_lost_version_restarts_higher_and_old_entries_are_not_reused(rig):
    user = rig.new_user()
    cached_once(rig, user)
    old_version = rig.rig.cache.corpus_version()
    rig.rig.redis.delete("corpus:version")  # Upstash lost the counter
    new_version = rig.rig.cache.corpus_version()
    assert new_version > 1_000_000_000_000 and new_version >= old_version  # from the clock, in ms
    again = run(rig.ask(QUESTION, user=user))
    assert not again.cached  # the old entry sits under another version: unreachable


# ---- 10.1 / 10.2: Upstash down during an ingest --------
class Down:
    """Every command is a refused connection."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise redis.exceptions.ConnectionError("connection refused")

        return fail

    def pipeline(self, *args, **kwargs):
        raise redis.exceptions.ConnectionError("connection refused")


def audit_rows(rig):
    return rig.rig.db.query_one("SELECT COUNT(*) FROM audit_log")[0]


def stored_payloads(rig, doc_id="doc-a"):
    return {i: dict(p["payload"]) for i, p in rig.rig.points(doc_id).items()}


@pytest.mark.parametrize("change", ["an edited chunk", "a new chunk", "who may see it"])
def test_an_ingest_refuses_to_start_when_upstash_does_not_accept_staleness_writes(rig, change):
    """Phase 9 [P] preflight: nothing is changed while the cache cannot be told about it."""
    user = rig.new_user()
    old_key = cached_once(rig, user)
    before, audits = stored_payloads(rig), audit_rows(rig)
    embedded = len(rig.rig.embedder.calls)
    working = rig.rig.cache._client
    rig.rig.cache._client = Down()
    edited, options = {
        "an edited chunk": (document(waiting="45 days"), {}),
        "a new chunk": (document(extra=True), {}),
        "who may see it": (document(), {"access_role": "staff"}),  # no chunk text changes
    }[change]
    with pytest.raises(CacheUnavailable, match="cache unavailable, nothing was changed"):
        rig.rig.ingest(edited, **options)
    assert stored_payloads(rig) == before  # no chunk was written, changed or deactivated
    assert not pending.is_pending(rig.rig.ctx.marker_dir, "doc-a")
    assert audit_rows(rig) == audits  # and no audit row
    assert len(rig.rig.embedder.calls) == embedded  # no model call was paid for
    rig.rig.cache._client = working  # Upstash is back
    rig.rig.cache._skip_until = 0.0
    assert rig.rig.redis.exists(old_key)  # untouched
    result = rig.rig.ingest(edited, **options)  # the same command, run again
    assert result.complete and not rig.rig.redis.exists(old_key)


def test_a_delete_refuses_to_start_when_upstash_does_not_accept_staleness_writes(rig):
    user = rig.new_user()
    old_key = cached_once(rig, user)
    before, audits = stored_payloads(rig), audit_rows(rig)
    working = rig.rig.cache._client
    rig.rig.cache._client = Down()
    with pytest.raises(CacheUnavailable, match="nothing was changed"):
        delete_document(rig.rig.ctx, "doc-a")
    assert stored_payloads(rig) == before and len(rig.rig.active()) == 3  # still all there
    assert not pending.is_pending(rig.rig.ctx.marker_dir, "doc-a")
    assert audit_rows(rig) == audits
    rig.rig.cache._client = working
    rig.rig.cache._skip_until = 0.0
    assert delete_document(rig.rig.ctx, "doc-a").complete and not rig.rig.redis.exists(old_key)


def test_a_review_approval_refuses_to_start_when_upstash_does_not_accept_staleness_writes(rig):
    rig.rig.ctx.enrich = enricher_for(rig.rig, Tools())  # the real scan holds a chunk back
    rig.rig.ingest(parsed(*poisoned()), doc_id="doc-p")
    held = [i for i, p in stored_payloads(rig, "doc-p").items() if p.get("quarantine") == "pending"]
    assert len(held) == 1, "one chunk is held back: the test proves something"
    working = rig.rig.cache._client
    rig.rig.cache._client = Down()
    asked = []
    with pytest.raises(CacheUnavailable, match="nothing was changed"):
        review_chunks(rig.rig.ctx, "doc-p", lambda prompt: asked.append(prompt) or "a", print)
    assert asked == []  # no decision was even asked for
    payload = stored_payloads(rig, "doc-p")[held[0]]
    assert payload["quarantine"] == "pending" and payload["effective_to"]  # still held back
    assert not pending.is_pending(rig.rig.ctx.marker_dir, "doc-p")
    rig.rig.cache._client = working
    rig.rig.cache._skip_until = 0.0
    done = review_chunks(rig.rig.ctx, "doc-p", lambda prompt: "a", lambda line: None)
    assert done.approved == 1 and not done.incomplete


def test_the_residual_window_a_bump_that_fails_after_the_preflight_is_incomplete(rig):
    """KNOWN, RECORDED LIMITATION (README, Phase 9): Upstash can fail between the preflight and
    the bump. The run then reports incomplete and keeps its marker; until the repeat, the version
    is unchanged, so an answer that cited the document (and not the new chunk) is still served."""
    user = rig.new_user()
    old_key = cached_once(rig, user)
    cache = rig.rig.cache
    # the preflight passed, then Upstash failed: the bump AND the invalidations are not done
    cache.bump_corpus_version = lambda: None
    cache.invalidate_doc = lambda doc_id: None
    cache.invalidate_chunk = lambda chunk_id: None
    result = rig.rig.ingest(document(extra=True))  # a NEW chunk
    assert result.new == 1 and not result.complete and result.incomplete
    assert pending.is_pending(rig.rig.ctx.marker_dir, "doc-a")
    assert "cache_staleness_write_failed" not in rig.rig.events  # (the bump was stubbed out)
    assert rig.rig.redis.exists(old_key)  # nothing invalidated the old entry yet
    window = run(rig.ask(QUESTION, user=user))
    assert window.cached  # the window: served although the document has a new chunk
    for name in ("bump_corpus_version", "invalidate_doc", "invalidate_chunk"):
        delattr(cache, name)  # Upstash is back; the same command again
    finished = rig.rig.ingest(document(extra=True))
    assert finished.complete and finished.status == "finished"
    assert not rig.rig.redis.exists(old_key)
    assert not pending.is_pending(rig.rig.ctx.marker_dir, "doc-a")
    assert not run(rig.ask(QUESTION, user=user)).cached


# ---- 10.3 item 7: a change of the role map makes every old entry unreachable --------
def test_changing_the_role_map_makes_old_entries_unreachable_and_restoring_it_brings_them_back(
    rig, monkeypatch
):
    user = rig.new_user()
    key = cached_once(rig, user)
    assert run(rig.ask(QUESTION, user=user)).cached  # a normal hit under today's map
    answers = len(rig.gateway.calls_of("complete", what="answer"))
    original = role_map_version()
    monkeypatch.setitem(VISIBLE_ROLES, "customer", ("customer", "staff"))  # the map changes
    assert role_map_version() != original
    miss = run(rig.ask(QUESTION, user=user))
    assert not miss.cached  # every old entry is unreachable ...
    assert len(rig.gateway.calls_of("complete", what="answer")) == answers + 1
    assert rig.rig.redis.exists(key)  # ... (not deleted: they just no longer match a key)
    monkeypatch.undo()  # the map is back
    assert role_map_version() == original
    assert run(rig.ask(QUESTION, user=user)).cached  # so the old entry is reachable again


def test_the_role_map_hash_is_part_of_the_key_the_pipeline_looks_up(rig):
    ctx_keys = []
    original = rig.pipeline.graph.ainvoke

    async def spy(state, config=None):
        out = await original(state, config)
        ctx_keys.append(state["ctx"].cache_key)
        return out

    rig.pipeline.graph.ainvoke = spy
    run(rig.ask(QUESTION))
    from app.cache.store import exact_key
    from app.guardrails.prompts import prompt_set_version

    version = rig.rig.cache.corpus_version()
    text = "What is the waiting period for illness claims?"
    with_map = exact_key(text, "customer", version, prompt_set_version(), role_map_version())
    without = exact_key(text, "customer", version, prompt_set_version())
    assert ctx_keys[0] == with_map and ctx_keys[0] != without
