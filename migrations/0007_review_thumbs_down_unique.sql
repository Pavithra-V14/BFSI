-- Phase 10 hardening. [P] One thumbs-down review item per answer, enforced by the database.
--
-- Before this, the API checked "is there already an item" and then inserted, so two requests at the
-- same moment (a double click, on Postgres) could add two items for one answer. The partial unique
-- index makes the second insert a no-op (the API uses ON CONFLICT DO NOTHING).
--
-- Note on migration 0004: its header says only the owner's admin command sets users.is_active to
-- false. Since Phase 10 an admin can also switch a customer or staff account off through the API
-- (never an admin account). Migrations are not edited after release, so the correction is here.
--
-- If an earlier double click already made two items for one answer, keep the first (lowest id) and
-- remove the others, so the index can be created.
DELETE FROM review_queue
WHERE kind = 'thumbs_down'
    AND message_id IS NOT NULL
    AND id NOT IN (
        SELECT MIN(id) FROM review_queue
        WHERE kind = 'thumbs_down' AND message_id IS NOT NULL
        GROUP BY message_id
    );

CREATE UNIQUE INDEX review_queue_thumbs_down_message
    ON review_queue (message_id) WHERE kind = 'thumbs_down';
