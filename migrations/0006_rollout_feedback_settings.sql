-- Phase 10 (chat, session and admin API). [P] Field details of section 12.1 are proposed.
--
-- messages.shadowed: an assistant answer that was generated while the rollout stage did not let
-- its user see answers (spec 19: shadow mode). The turn is stored in full for evaluation, but the
-- answer stays hidden from that user for good, also after the stage opens.
ALTER TABLE messages ADD COLUMN shadowed BOOLEAN NOT NULL DEFAULT FALSE;

-- app_settings: small named settings an admin can change without a redeploy. Today one key,
-- "rollout_stage" (shadow, staff_beta or open). A missing row means "shadow" (fail closed).
CREATE TABLE app_settings (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL,
    updated_by TEXT REFERENCES users (id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- One thumbs entry per user per message (the Phase 3 finding assigned to Phase 10): giving the
-- other thumb changes the entry, it does not add one.
CREATE UNIQUE INDEX feedback_entries_message_user ON feedback_entries (message_id, user_id);
