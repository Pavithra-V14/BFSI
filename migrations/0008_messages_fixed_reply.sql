-- Phase 11 hardening. [P] messages.fixed_reply: an assistant message that is one of our own fixed
-- texts (a greeting, a refusal, the low-confidence text, the unclear-turn ending) and not a
-- generated answer. A fixed reply is always stored with grounded = FALSE, so "not grounded" alone
-- cannot tell a failed verification from a fixed text. The chat screen shows its "not fully
-- verified" notice and the thumbs only for generated answers, so it needs this marker.
--
-- Messages stored before this migration keep the default (FALSE), so an old greeting or refusal
-- still looks like an answer on resume. They cannot be told apart afterwards without guessing,
-- and nothing is guessed here. New messages are marked as they are written.
ALTER TABLE messages ADD COLUMN fixed_reply BOOLEAN NOT NULL DEFAULT FALSE;
