"""The record of withdrawn documents (Phase 15 round 3, migration 0010) [P].

A document that was deleted on purpose must stay gone through a restore. The delete command
writes its id here (`record`), moves its original PDFs out of the way (`move_originals`), and a
restore re-runs the delete for every id listed by `app.ingest withdrawn`. An ingest of a withdrawn
id is refused (`refuse_if_withdrawn`), so a row here never contradicts a live document, unless the
owner adds `--reinstate` (`reinstate`: the row is removed and an audit row written).

The table holds the id, the time and a fixed reason code: no text.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from app.accounts.audit import write_audit
from app.accounts.tokens import utc_text
from app.db.connection import Database
from app.ingest.model import WITHDRAW_REASONS, IngestError, check_choice, check_doc_id

INSERT_SQL = (
    "INSERT INTO withdrawn_documents (doc_id, withdrawn_at, reason_code) VALUES (?, ?, ?) "
    "ON CONFLICT (doc_id) DO NOTHING"
)
WITHDRAWN_FOLDER = "_withdrawn"  # inside originals/; never a valid document id (no underscore)


def record(db: Database, doc_id: str, reason: str, now: datetime) -> bool:
    """Remember that the document was withdrawn. The caller commits. A second call keeps the first
    time and reason. Returns True when this call added the row."""
    check_doc_id(doc_id)
    check_choice(reason, WITHDRAW_REASONS, "reason")
    cursor = db.execute(INSERT_SQL, (doc_id, utc_text(now), reason))
    return bool(cursor.rowcount and cursor.rowcount > 0)


def is_withdrawn(db: Database, doc_id: str) -> bool:
    return db.query_one("SELECT 1 FROM withdrawn_documents WHERE doc_id = ?", (doc_id,)) is not None


def list_withdrawn(db: Database) -> list[tuple[str, str, str]]:
    """(doc_id, withdrawn_at, reason_code), oldest first."""
    rows = db.query_all(
        "SELECT doc_id, withdrawn_at, reason_code FROM withdrawn_documents "
        "ORDER BY withdrawn_at, doc_id"
    )
    return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]


def refuse_if_withdrawn(db: Database, doc_id: str) -> None:
    """An ingest of a withdrawn id would make a restore delete a live document: refuse it."""
    if is_withdrawn(db, doc_id):
        raise IngestError(
            f"the document id '{doc_id}' was withdrawn: use a new id for a new document, or add "
            "--reinstate ID --confirm-id ID to bring this id back on purpose (the record is "
            "removed and an audit row written). A restore never does that by itself."
        )


def reinstate(
    db: Database,
    doc_id: str,
    *,
    asked_id: str | None = None,
    confirm_id: str | None = None,
    actor: str,
    now: datetime,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Take an id off the withdrawn list on purpose (`ingest --reinstate`), with one audit row.
    Returns True when a record was removed, False when the id was not withdrawn (nothing is
    written then). Only the owner's own command calls this; no restore path does."""
    check_doc_id(doc_id)
    from app.gateway.embeddings import running_on_render

    if running_on_render(os.environ if env is None else env):
        raise IngestError(
            "--reinstate is refused on the hosted service: it is an admin-machine act"
        )
    if asked_id != doc_id or confirm_id != doc_id:
        raise IngestError(
            "--reinstate needs the document id twice, both equal to --doc-id: "
            f"--reinstate {doc_id} --confirm-id {doc_id}"
        )
    row = db.query_one("SELECT reason_code FROM withdrawn_documents WHERE doc_id = ?", (doc_id,))
    if row is None:
        return False
    with db.transaction(write=True):
        db.execute("DELETE FROM withdrawn_documents WHERE doc_id = ?", (doc_id,))
        write_audit(
            db,
            actor=actor,
            action="reinstate",
            target=doc_id,
            details={"reason": str(row[0])},
            now=now,
        )
    return True


def move_originals(originals_dir: Path, doc_id: str) -> int:
    """Move `originals/<doc_id>/*.pdf` to `originals/_withdrawn/<doc_id>/`, so a re-ingest from
    `originals/` can never bring the document back. Returns how many files were moved. Safe to
    repeat; a file already there with the same name (the same content: the name is its SHA-256)
    is replaced."""
    check_doc_id(doc_id)
    source = originals_dir / doc_id
    if not source.is_dir():
        return 0
    target = originals_dir / WITHDRAWN_FOLDER / doc_id
    moved = 0
    for pdf in sorted(source.glob("*.pdf")):
        target.mkdir(parents=True, exist_ok=True)
        pdf.replace(target / pdf.name)
        moved += 1
    try:
        source.rmdir()  # only when empty: anything else in the folder is left alone
    except OSError:
        pass
    return moved
