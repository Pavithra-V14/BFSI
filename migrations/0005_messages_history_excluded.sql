-- Whether a message must stay out of the history and the running summary (spec 8.2, 8.6).
-- A user question that tripped the injection scan, or whose answer was a refusal, is stored (the
-- transcript is complete) but flagged here, and never feeds the rewrite step or the summary.
-- [P] Field details of section 12.1 are proposed.
ALTER TABLE messages ADD COLUMN history_excluded BOOLEAN NOT NULL DEFAULT FALSE;
