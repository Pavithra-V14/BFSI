# Runbook: a key leaked

**When:** A provider key, the database address, the chat webhook address or a signing secret was pasted, committed, logged or shown anywhere it should not be.

**Who does it:** The admin, at once.

## Steps
1. **Do not wait to be sure.** Treat it as leaked.
2. **Make a new one** at the provider (Groq, Google, OpenRouter, Cohere, Jina, Qdrant, Upstash,
   Neon, Langfuse) or, for `JWT_SECRET_KEY`, `AUDIT_HASH_KEY`, `LANGFUSE_USER_HASH_KEY`, with
   `uv run python -c "import secrets; print(secrets.token_urlsafe(32))"` in your own terminal.
3. **Put it in with no downtime** (the order in `docs/KEY_ROTATION.md`): for a list of keys, add
   the new one, save, wait for the restart, then remove the old one and save again. For a single
   setting, replace it and save.
4. **Revoke the old one** at the provider. This is the step that closes the leak.
5. **Special cases:**
   - `JWT_SECRET_KEY`: replacing it signs every user out (their tokens no longer verify). That is
     the point.
   - `AUDIT_HASH_KEY`: replacing it changes the hashes of usernames and addresses from now on;
     older rows cannot be matched to new ones. Accept that.
   - `ALERT_WEBHOOK_URL`: delete the webhook in the chat tool and make a new one.
   - A key committed to git: rotating is not enough, the history keeps it; run the secret scan
     (Run 24) and tell your helper.
6. Run `uv run python scripts/key_inventory.py` with the new values: the fingerprint changed and
   no key appears twice.

## Check
- The provider's console shows the old key revoked.
- `/health` is healthy and a question is answered.

## Afterwards
Write the date and the reason in `docs/KEY_ROTATION.md`'s log table. Search the chat tool,
tickets and logs for the old key's first characters.

## Last rehearsal
Never rehearsed. Write the date, who did it and how long it took in `docs/runbooks/README.md`
after each rehearsal.
