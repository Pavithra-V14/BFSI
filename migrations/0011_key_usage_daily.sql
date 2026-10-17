-- [P] key_usage_daily: how many provider calls and tokens each API key made on each UTC day.
--
-- Why: the admin key panel shows, per key, the calls made today and the calls left of the daily
-- limit. The gateway counts in memory and the observability rollup adds the counts here every few
-- minutes, so a restart (the free host sleeps and wakes) does not lose today's numbers.
--
-- It holds NO key: `fingerprint` is the first eight hex characters of the key's SHA-256, the same
-- short name the logs and traces use. One row per day, provider pool and key.
CREATE TABLE key_usage_daily (
    id TEXT NOT NULL PRIMARY KEY,
    day TEXT NOT NULL,
    provider TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX key_usage_daily_unique ON key_usage_daily (day, provider, fingerprint);
