-- Sessions and messages (spec 12.1, with the session columns of 8.3 and the message flags of 8.5).
-- Field details are [P]. Message text is always masked text (spec invariant).

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- 8.3: the running summary, built from masked text only.
    summary_text TEXT,
    -- The id of the last message folded into the summary. No foreign key: messages point back
    -- at sessions, and a deleted message must not block anything.
    summary_upto_message_id TEXT,
    -- Structured state as JSON text, filled without an extra LLM call [P].
    session_facts TEXT NOT NULL DEFAULT '{}',
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'active',  -- [P]
    CONSTRAINT sessions_status_check CHECK (status IN ('active', 'closed'))
);

-- A user's session list, and the 90-day retention sweep (spec 8.4).
CREATE INDEX sessions_user_last_active ON sessions (user_id, last_active_at);
CREATE INDEX sessions_last_active ON sessions (last_active_at);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    -- Deleting a session (a user deleting their own chat, or retention) removes its messages.
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    -- [P] Position of the message inside its session. Order never depends on how ids are made.
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    citations TEXT NOT NULL DEFAULT '[]',  -- JSON text
    grounded BOOLEAN,  -- unknown (NULL) for user messages
    -- 8.5: the cited document was deleted.
    source_removed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT messages_role_check CHECK (role IN ('user', 'assistant')),
    CONSTRAINT messages_session_seq UNIQUE (session_id, seq)
);
