-- Accounts (spec 12.1). Field details are [P].
-- One SQL set for Postgres and SQLite: text UUID ids, JSON kept as text, TIMESTAMPTZ columns.
-- The application always writes UTC timestamps.

CREATE TABLE users (
    id TEXT NOT NULL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    -- Registration never sets a role: new accounts are customers (spec 13.1).
    role TEXT NOT NULL DEFAULT 'customer',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT users_role_check CHECK (role IN ('customer', 'staff', 'admin'))
);
-- username and email are normalised by the application with one fixed rule (strip, then
-- casefold: app/db/identity.py) before they are stored or looked up. The database does not fold
-- case itself, because lower() gives different answers on Postgres and on SQLite.

-- A revoked JWT, kept until it would have expired anyway (spec 13.1).
CREATE TABLE revoked_tokens (
    jti TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    revoked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX revoked_tokens_expires_at ON revoked_tokens (expires_at);
