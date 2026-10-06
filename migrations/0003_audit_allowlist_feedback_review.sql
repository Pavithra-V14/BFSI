-- Audit log, entity allow-list, feedback and the human-review queue (spec 12.1, 6.8, 11.4).
-- Field details are [P], except the allow-list, which has exactly the columns the spec lists.

-- Append-only: the application only ever inserts, and the 30-day clean-up deletes old rows.
-- That rule is enforced in code, not by a database trigger, because a trigger cannot be written
-- once for both Postgres and SQLite. No document text goes into details (spec 6.7).
CREATE TABLE audit_log (
    id TEXT NOT NULL PRIMARY KEY,
    actor TEXT NOT NULL,  -- who: a user id, or 'system'
    action TEXT NOT NULL,  -- what
    target TEXT,  -- for example a document id or a user id
    details TEXT NOT NULL DEFAULT '{}',  -- JSON text
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP  -- when
);

CREATE INDEX audit_log_created_at ON audit_log (created_at);

-- Entity allow-list (spec 6.8): terms that must not be masked. Tied to a document id, which
-- lives in the vector store, so it is plain text here.
CREATE TABLE allowlist_entities (
    id TEXT NOT NULL PRIMARY KEY,
    doc_id TEXT NOT NULL,
    text TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    approved_by TEXT REFERENCES users (id) ON DELETE SET NULL,
    approved_at TIMESTAMPTZ,
    access_role TEXT NOT NULL,
    CONSTRAINT allowlist_status_check CHECK (status IN ('pending', 'approved', 'rejected'))
);

CREATE INDEX allowlist_entities_doc_id ON allowlist_entities (doc_id);
CREATE INDEX allowlist_entities_status ON allowlist_entities (status);

-- Thumbs on an answer (spec 11.4). [P]
CREATE TABLE feedback_entries (
    id TEXT NOT NULL PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    rating TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT feedback_rating_check CHECK (rating IN ('up', 'down'))
);

CREATE INDEX feedback_entries_message_id ON feedback_entries (message_id);

-- The admin review queue (spec 11.4). [P] The overdue limit is [T] and is worked out from
-- created_at, so there is no column for it. Details hold masked text only.
CREATE TABLE review_queue (
    id TEXT NOT NULL PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    session_id TEXT REFERENCES sessions (id) ON DELETE SET NULL,
    message_id TEXT REFERENCES messages (id) ON DELETE SET NULL,
    doc_id TEXT,
    details TEXT NOT NULL DEFAULT '{}',  -- JSON text, masked
    label TEXT,
    labelled_by TEXT REFERENCES users (id) ON DELETE SET NULL,
    labelled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT review_queue_kind_check CHECK (
        kind IN (
            'refused',
            'low_confidence',
            'thumbs_down',
            'two_unclear_turns',
            'failed_ingest_page'
        )
    ),
    CONSTRAINT review_queue_status_check CHECK (
        status IN ('open', 'labelled', 'added_to_eval', 'dismissed')
    )
);

CREATE INDEX review_queue_status_created ON review_queue (status, created_at);
