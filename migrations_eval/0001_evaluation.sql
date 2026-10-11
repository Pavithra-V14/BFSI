-- Phase 13 (evaluation). [P] Field details are proposed; [T] the choice of database for the
-- production environment is to be confirmed (spec 12.2).
--
-- This is the SEPARATE evaluation database (spec 12.2, 15.1). It is never the main database: the
-- local environment is a SQLite file on the admin machine, the production environment is its own
-- cloud Postgres database (EVAL_DATABASE_URL). Applied with `python -m app.eval migrate`.
--
-- eval_cases: the labelled evaluation set. The question text is written by the project from the
-- sample corpus (no real customer data), or is the masked text of a review-queue item that an
-- admin added (status 'candidate' until a new dataset version is cut). Expected sources are stored
-- by clause reference (and an anchor phrase), never by chunk id (spec 15.2).
CREATE TABLE eval_cases (
    id TEXT NOT NULL PRIMARY KEY,
    dataset_version TEXT NOT NULL,
    category TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'set',
    role TEXT NOT NULL,
    turns TEXT NOT NULL,
    expect TEXT NOT NULL,
    setup TEXT,
    source_docs TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'authored',
    review_item_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT eval_cases_status_check CHECK (status IN ('set', 'candidate', 'retired'))
);

CREATE INDEX eval_cases_dataset ON eval_cases (dataset_version, status);

-- eval_runs: one row per run. `estimate` holds the numbers the owner confirmed (requests and
-- tokens per provider against the daily limits); `confirmed_at` is set only by an explicit
-- confirmation of that estimate. A run with status 'aborted_over_estimate' went more than 25 %
-- over it and stopped.
CREATE TABLE eval_runs (
    id TEXT NOT NULL PRIMARY KEY,
    environment TEXT NOT NULL,
    kind TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    system_version TEXT NOT NULL,
    status TEXT NOT NULL,
    estimate TEXT NOT NULL,
    estimate_code TEXT NOT NULL,
    confirmed_at TIMESTAMPTZ,
    planned_items INTEGER NOT NULL DEFAULT 0,
    finished_items INTEGER NOT NULL DEFAULT 0,
    batch_no INTEGER NOT NULL DEFAULT 1,
    batches INTEGER NOT NULL DEFAULT 1,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    requests_used INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    note TEXT,
    CONSTRAINT eval_runs_environment_check CHECK (environment IN ('local', 'production')),
    CONSTRAINT eval_runs_kind_check CHECK (
        kind IN ('active', 'full', 'variance', 'calibration', 'samples')
    )
);

CREATE INDEX eval_runs_created_at ON eval_runs (created_at);

-- eval_items: the checkpoint of a run. One row per case; a resumed run skips the finished ones.
-- The answer text is the system's answer to a question WE wrote (or a masked sample), so it holds
-- no customer data. `contexts` are the chunk texts that reached the generator (public sample
-- documents), kept so that a judge can be run again without asking the system again.
CREATE TABLE eval_items (
    id TEXT NOT NULL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES eval_runs (id) ON DELETE CASCADE,
    case_id TEXT NOT NULL,
    status TEXT NOT NULL,
    answer_kind TEXT,
    reason TEXT,
    answer_text TEXT,
    chunk_ids TEXT,
    clause_refs TEXT,
    doc_ids TEXT,
    contexts TEXT,
    models TEXT,
    cheap_checks TEXT,
    tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms DOUBLE PRECISION,
    finished_at TIMESTAMPTZ,
    CONSTRAINT eval_items_status_check CHECK (status IN ('pending', 'done', 'failed'))
);

CREATE UNIQUE INDEX eval_items_unique ON eval_items (run_id, case_id);

-- eval_results: one score per run, case and metric. `scored` is false when nothing could score
-- it ("not scored", spec 5.1); the score is then NULL, never a made-up number.
CREATE TABLE eval_results (
    id TEXT NOT NULL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES eval_runs (id) ON DELETE CASCADE,
    case_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    score DOUBLE PRECISION,
    scored BOOLEAN NOT NULL DEFAULT TRUE,
    decider TEXT NOT NULL,
    model TEXT,
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX eval_results_unique ON eval_results (run_id, case_id, metric);

-- eval_samples: the production sample (about 5 % of live answers), written by the running service
-- off the request path. MASKED text only (the same masked copies the transcripts hold), the text
-- of the passages that reached the generator (public company documents), and cheap code checks. Judged later on the admin machine, with the evaluation keys. Purged after 30 days
-- by the owner-run retention command.
CREATE TABLE eval_samples (
    id TEXT NOT NULL PRIMARY KEY,
    sampled_at TIMESTAMPTZ NOT NULL,
    role TEXT NOT NULL,
    result_kind TEXT NOT NULL,
    reason TEXT,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    chunk_ids TEXT,
    clause_refs TEXT,
    contexts TEXT,
    models TEXT,
    prompt_hash TEXT,
    corpus_version TEXT,
    confidence TEXT,
    degraded TEXT,
    cheap_checks TEXT,
    tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms DOUBLE PRECISION,
    judged_at TIMESTAMPTZ,
    run_id TEXT
);

CREATE INDEX eval_samples_sampled_at ON eval_samples (sampled_at);

-- eval_usage_days: what the runs used of each provider group on each UTC day, so a second batch
-- or a second run on the same day is held back when the free-tier daily limits would be passed.
CREATE TABLE eval_usage_days (
    day TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES eval_runs (id) ON DELETE CASCADE,
    who TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX eval_usage_days_unique ON eval_usage_days (day, run_id, who);

-- eval_drift_snapshots: the weekly drift report, so a later week has a baseline window.
CREATE TABLE eval_drift_snapshots (
    id TEXT NOT NULL PRIMARY KEY,
    environment TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    baseline_start TIMESTAMPTZ,
    baseline_end TIMESTAMPTZ,
    measures TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- eval_calibration: the judge-against-labels agreement per metric. Jev is used for a metric only
-- when `trusted` (agreement at or above the threshold) (spec 15.3).
CREATE TABLE eval_calibration (
    metric TEXT NOT NULL PRIMARY KEY,
    items INTEGER NOT NULL,
    judge_agreement DOUBLE PRECISION,
    jev_agreement DOUBLE PRECISION,
    threshold DOUBLE PRECISION NOT NULL,
    trusted BOOLEAN NOT NULL DEFAULT FALSE,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
