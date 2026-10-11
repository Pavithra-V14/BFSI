-- Phase 12 (observability). [P] Field details are proposed. Counts and names only: no text, no user
-- id and no session id is stored in any of these tables.
--
-- alert_events: one row each time an alert fires (a rule over a window, or a critical event). Only
-- the alert's name, its severity, a count and the time. `notified` is true when the chat webhook
-- was sent for it (the daily cap on webhook messages is counted from it). Kept 30 days.
CREATE TABLE alert_events (
    id TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    severity TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 1,
    notified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT alert_events_severity_check CHECK (severity IN ('warning', 'critical'))
);

CREATE INDEX alert_events_created_at ON alert_events (created_at);

-- metric_rollups: hourly and daily numbers for the dashboards, per role (customer, staff or all).
-- Numbers only. Hourly rows are added to as requests happen (so a restart loses nothing and counts
-- nothing twice); daily rows are counts of the UTC day.
CREATE TABLE metric_rollups (
    id TEXT NOT NULL PRIMARY KEY,
    period TEXT NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    role TEXT NOT NULL,
    metric TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL DEFAULT 0,
    CONSTRAINT metric_rollups_period_check CHECK (period IN ('hour', 'day')),
    CONSTRAINT metric_rollups_role_check CHECK (role IN ('customer', 'staff', 'all'))
);

CREATE UNIQUE INDEX metric_rollups_unique
    ON metric_rollups (period, period_start, role, metric);

-- trace_deletions: the chats whose Langfuse traces still have to be removed (spec 13.6). The row
-- holds only the keyed hash the traces carry as their session id. It goes when the traces are gone.
CREATE TABLE trace_deletions (
    id TEXT NOT NULL PRIMARY KEY,
    session_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    alerted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_attempt_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX trace_deletions_session_hash ON trace_deletions (session_hash);
