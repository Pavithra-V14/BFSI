-- Phase 15 round 3. [P] withdrawn_documents: the ids of documents that were withdrawn on purpose.
--
-- Why: a restore (from a Qdrant snapshot, or a re-ingest from `originals/`) must never bring a
-- withdrawn document back. The delete command writes one row here, in the same transaction as the
-- rest of its work, and a restore re-runs the delete for every id in this table before the system
-- is called restored (docs/runbooks/restore-documents.md). A new ingest of a withdrawn id is
-- refused, so a row never contradicts a live document.
--
-- Permanent: no clean-up job touches it. It holds NO text: the id, the time and a fixed reason
-- code. Who withdrew it is in the audit row of the delete.
CREATE TABLE withdrawn_documents (
    doc_id TEXT NOT NULL PRIMARY KEY,
    withdrawn_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reason_code TEXT NOT NULL,
    CONSTRAINT withdrawn_documents_reason_check
        CHECK (reason_code IN ('outdated', 'incorrect', 'compliance', 'replaced', 'other'))
);
