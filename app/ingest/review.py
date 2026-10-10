"""The owner's review, in the terminal (spec 6.8 step 5, 9.3).

`review entities`: pending allow-list entries, in bulk. A person is shown once, with the context
the name appears in and the variants that go with it; one answer approves the group. A
single-word variant (the surname alone) is approved only if the reviewer ticks it.

`review chunks`: chunks the injection scan held back. Approving makes the chunk active (and bumps
the corpus version and clears the document's cache entries, like any new chunk); rejecting keeps it
inactive for good (it will not be recreated by a later run of the same text).

Every session writes one audit row with counts only. Nothing is shown from, or written about, any
document other than the one named.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.accounts.audit import write_audit
from app.accounts.tokens import utc_text
from app.ingest import pending
from app.ingest.entities import normalise_entity, person_variants
from app.ingest.index import IngestContext, load_existing
from app.ingest.lock import document_lock
from app.ingest.model import check_doc_id
from app.store_errors import call_vector
from app.vector.documents import checked_update

SNIPPET_CHARS = 240


@dataclass
class Row:
    id: str
    text: str
    kind: str


@dataclass
class Group:
    head: str
    kind: str
    rows: list[Row] = field(default_factory=list)
    snippet: str = ""

    def single_word(self, row: Row) -> bool:
        return self.kind == "person_in_policy" and len(row.text.split()) == 1


_INITIAL = ("mr", "mrs", "ms", "dr", "shri", "smt", "prof")


def _is_head(row: Row) -> bool:
    words = row.text.split()
    if row.kind != "person_in_policy" or len(words) < 2:
        return False
    first = words[0].rstrip(".").casefold()
    return first not in _INITIAL and len(words[0].rstrip(".")) > 1


def build_groups(rows: list[Row]) -> list[Group]:
    """Group pending rows: a full person name with its generated variants; others alone."""
    taken: set[str] = set()
    groups: list[Group] = []
    by_text = {normalise_entity(r.text): r for r in rows}
    for row in rows:
        if not _is_head(row) or row.id in taken:
            continue
        group = Group(row.text, row.kind)
        for variant in person_variants(row.text):
            member = by_text.get(normalise_entity(variant.text))
            if member is not None and member.id not in taken:
                taken.add(member.id)
                group.rows.append(member)
        groups.append(group)
    for row in rows:
        if row.id not in taken:
            groups.append(Group(row.text, row.kind, [row]))
    return groups


def _snippet(texts: list[str], term: str) -> str:
    needle = term.casefold()
    for text in texts:
        position = text.casefold().find(needle)
        if position >= 0:
            around = text[max(0, position - SNIPPET_CHARS // 2) : position + SNIPPET_CHARS // 2]
            return " ".join(around.split())
    return ""


@dataclass
class ReviewResult:
    approved: int = 0
    rejected: int = 0
    skipped: int = 0


def _review_entities(
    ctx: IngestContext,
    doc_id: str,
    ask: Callable[[str], str],
    out: Callable[[str], None],
) -> ReviewResult:
    check_doc_id(doc_id)
    db, result = ctx.db, ReviewResult()
    rows = [
        Row(*row)
        for row in db.query_all(
            "SELECT id, text, entity_type FROM allowlist_entities "
            "WHERE doc_id = ? AND status = 'pending' ORDER BY entity_type, text",
            (doc_id,),
        )
    ]
    if not rows:
        out("nothing is pending for this document")
        return result
    chunk_texts = [
        str(e.payload.get("text") or "")
        for e in load_existing(ctx.qdrant, ctx.collection, doc_id)
        if e.payload.get("text")
    ]
    groups = build_groups(rows)
    approve_rest = False
    now = utc_text(ctx.now())

    def set_status(ids: list[str], status: str) -> None:
        with db.transaction(write=True):
            for row_id in ids:
                db.execute(
                    "UPDATE allowlist_entities SET status = ?, approved_by = NULL, "
                    "approved_at = ? WHERE id = ?",
                    (status, now if status == "approved" else None, row_id),
                )

    for number, group in enumerate(groups, start=1):
        group.snippet = _snippet(chunk_texts, group.head)
        out(f"\n[{number}/{len(groups)}] {group.kind}: {group.head}")
        if group.snippet:
            out(f"  context: ...{group.snippet}...")
        for row in group.rows:
            note = "  (single word: approved only if you tick it)" if group.single_word(row) else ""
            out(f"  - {row.text}{note}")
        multi = [r.id for r in group.rows if not group.single_word(r)]
        single = [r.id for r in group.rows if group.single_word(r)]
        if approve_rest:
            answer = "a"
        else:
            answer = ask(
                "[a]pprove group  [t]ick the single-word too  [r]eject  [s]kip  "
                "[A]pprove all the rest  [q]uit: "
            ).strip()
        if answer == "q":
            result.skipped += len(groups) - number + 1
            break
        if answer == "A":
            approve_rest, answer = True, "a"
        if answer in ("a", "t"):
            ids = multi + (single if answer == "t" else [])
            set_status(ids, "approved")
            result.approved += len(ids)
        elif answer == "r":
            set_status([r.id for r in group.rows], "rejected")
            result.rejected += len(group.rows)
        else:
            result.skipped += 1
    with db.transaction(write=True):
        write_audit(
            db,
            actor=ctx.actor,
            action="allowlist_review",
            target=doc_id,
            details={"approved": result.approved, "rejected": result.rejected},
            now=ctx.now(),
        )
    return result


@dataclass
class ChunkReview:
    approved: int = 0
    rejected: int = 0
    skipped: int = 0
    incomplete: list[str] = field(default_factory=list)


def review_chunks(
    ctx: IngestContext,
    doc_id: str,
    ask: Callable[[str], str],
    out: Callable[[str], None],
) -> ChunkReview:
    check_doc_id(doc_id)
    with document_lock(ctx.lock_dir, doc_id):
        return _review_chunks(ctx, doc_id, ask, out)


def review_entities(
    ctx: IngestContext,
    doc_id: str,
    ask: Callable[[str], str],
    out: Callable[[str], None],
) -> ReviewResult:
    check_doc_id(doc_id)
    with document_lock(ctx.lock_dir, doc_id):
        return _review_entities(ctx, doc_id, ask, out)


def _review_chunks(
    ctx: IngestContext,
    doc_id: str,
    ask: Callable[[str], str],
    out: Callable[[str], None],
) -> ChunkReview:
    result = ChunkReview()
    held = [
        e
        for e in load_existing(ctx.qdrant, ctx.collection, doc_id)
        if e.payload.get("quarantine") == "pending"
    ]
    # An unfinished earlier review is finished first, even when nothing is left to decide.
    redo = pending.is_pending(ctx.marker_dir, doc_id)
    if not held and not redo:
        out("no chunk is held back for this document")
        return result
    pending.require_cache(ctx.cache)  # before any decision is written
    now = ctx.now()
    for number, chunk in enumerate(held, start=1):
        payload = chunk.payload
        out(
            f"\n[{number}/{len(held)}] page {payload.get('page_start')}, clause "
            f"{payload.get('clause_ref') or '-'}, matched: {payload.get('quarantine_reason')}"
        )
        out("  " + " ".join(str(payload.get("text") or "").split())[:600])
        answer = ask("[a]pprove (make active)  [r]eject  [s]kip  [q]uit: ").strip()
        if answer == "q":
            result.skipped += len(held) - number + 1
            break
        if answer == "a":
            pending.mark(ctx.marker_dir, doc_id, now, "review")  # before the chunk goes live
            update = checked_update(
                {"effective_to": None, "quarantine": None, "quarantine_reason": None}, now=now
            )
            call_vector(
                lambda point=chunk.id, update=update: ctx.qdrant.set_payload(
                    ctx.collection, payload=update, points=[point], wait=True
                )
            )
            result.approved += 1
        elif answer == "r":
            update = checked_update({"quarantine": "rejected"}, now=now)
            call_vector(
                lambda point=chunk.id, update=update: ctx.qdrant.set_payload(
                    ctx.collection, payload=update, points=[point], wait=True
                )
            )
            result.rejected += 1
        else:
            result.skipped += 1
    if result.approved or redo:  # a chunk became active: like any new chunk (spec 10.3)
        result.incomplete = pending.finish_cache_steps(
            ctx.cache,
            ctx.marker_dir,
            doc_id,
            chunk_ids=[],
            whole_document=True,
            now=now,
            reason="review",
        )
    if result.approved or result.rejected or redo:
        with ctx.db.transaction(write=True):
            write_audit(
                ctx.db,
                actor=ctx.actor,
                action="chunk_review",
                target=doc_id,
                details={
                    "approved": result.approved,
                    "rejected": result.rejected,
                    "incomplete": bool(result.incomplete),
                    "finished_earlier_run": redo,
                },
                now=now,
            )
    return result
