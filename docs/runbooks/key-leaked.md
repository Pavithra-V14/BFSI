# Runbook: a key leaked

**When:** A provider key, the database address, the chat webhook address or a signing secret was pasted, committed, logged or shown anywhere it should not be.

**Who does it:** The admin, at once.

## Steps
1. **Do not wait to be sure.** Treat it as leaked.
2. **Make a new one** at the provider (Groq, Google, OpenRouter, Cohere, Jina, Qdrant, Upstash,
   Neon, Langfuse) or, for `JWT_SECRET_KEY`, `AUDIT_HASH_KEY`, `LANGFUSE_USER_HASH_KEY`, with
   `uv run python -c "import secrets; print(secrets.token_urlsafe(32))"` in your own terminal
   (the value appears only in that window: do not copy it anywhere but the next step).
3. **Put it in** (the order in `docs/KEY_ROTATION.md`). **Every save restarts the service** (about
   a minute, during which requests can fail or be slow): for a list of keys, add the new one,
   save, wait for the restart, then remove the old one and save again; for a single setting,
   replace it and save. Each save is one restart.
4. **Revoke the old one** at the provider. This is the step that closes the leak.
5. **Update the scheduled tasks' encrypted files** when the key is one of `DATABASE_URL`,
   `EVAL_DATABASE_URL`, `QDRANT_URL`, `QDRANT_API_KEY`, `LANGFUSE_USER_HASH_KEY`
   (`docs/DEPLOY.md` 9.1: run the two lines again with the same name). Otherwise the retention,
   drift and snapshot tasks keep using the revoked value and fail without a sound (only
   retention raises an alert, after seven days).
6. **Special cases:**
   - `JWT_SECRET_KEY`: replacing it signs every user out (their tokens no longer verify). That is
     the point.
   - `AUDIT_HASH_KEY`: replacing it changes the hashes of usernames and addresses from now on;
     older rows cannot be matched to new ones, and the login throttle counters start from zero.
     Accept that.
   - `LANGFUSE_USER_HASH_KEY`: replacing it changes the hashed chat names sent to Langfuse, so
     the traces of chats written before the change **can no longer be found by the chat's hash**
     and the deletion of an old chat's traces will not reach them. If the leak is not urgent, run
     the retention task once first (`docs/KEY_ROTATION.md`); on a real leak accept it.
   - `ALERT_WEBHOOK_URL`: delete the webhook in the chat tool and make a new one.
   - A key committed to git: rotating is not enough, the history keeps it; run the secret scan
     (Run 24) and tell your helper.
7. **Check what you hold, knowing what it proves.** `uv run python scripts/key_inventory.py` reads
   **only the variables of the window you run it in**, not Render's. Put the new values into that
   window first (`Read-Secret`, `docs/OWNER_RUNS.md` "Common setup"); then the fingerprints show
   that the value YOU hold is the new one and that no key appears twice. They do **not** show what
   Render holds: for that, ask one question and read `/health` (a rejected key is shown there).

## Check
- The provider's console shows the old key revoked.
- `/health` is healthy and a question is answered (this is what proves Render has the new value).

## Afterwards
Write the date and the reason in `docs/KEY_ROTATION.md`'s log table. Search the chat tool,
tickets and logs for the old key's first characters. Clear your window: close it, or
`Remove-Item Env:NAME` for each name you set.

## Last rehearsal
Never rehearsed. Write the date, who did it and how long it took in `docs/runbooks/README.md`
after each rehearsal.
