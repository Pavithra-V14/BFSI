# Key rotation routine

Spec 13.3: "secrets only in the host's secret store ... key rotation routine". This page says
what to rotate, how often, in what order, and where each key lives. A leaked key is rotated at
once (`docs/runbooks/key-leaked.md`); this page is for the routine.

## Where each secret lives

| Secret | Settings name | Lives on Render | Lives on your machine | Rotate how often [T] |
|---|---|---|---|---|
| Groq, Gemini, OpenRouter answer keys | `GROQ_API_KEYS`, `GEMINI_API_KEYS`, `OPENROUTER_API_KEYS` | yes (a comma list, up to four) | in the shell when you run the gateway smoke test | every 90 days, one key at a time |
| Jev key | `JEV_OPENROUTER_API_KEY` | yes | the shell of a smoke run | every 90 days |
| Embedding keys | `GEMINI_EMBEDDING_KEY_A`, `_B` | yes | the shell of an ingest | every 90 days, one at a time |
| Rerank keys | `COHERE_API_KEY`, `JINA_API_KEY` | yes | the shell of a smoke run | every 90 days |
| Evaluation keys | `EVAL_GROQ_API_KEYS`, `EVAL_GEMINI_API_KEYS`, `EVAL_OPENROUTER_API_KEYS` | **never** | the shell of an evaluation run | every 90 days |
| Vector store key | `QDRANT_API_KEY` | yes | the shell of an ingest or a snapshot | every 180 days |
| Cache address | `UPSTASH_REDIS_URL` | yes | the shell of the cache check | every 180 days |
| Database addresses | `DATABASE_URL`, `EVAL_DATABASE_URL` | yes | the shell of migrations and the retention task | every 180 days (the password inside) |
| Tracing keys | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | yes | only in a shell that runs the Langfuse smoke check (Run 13); the retention task does not need them | every 180 days |
| Signing secret | `JWT_SECRET_KEY` | yes | the shell of a local API run | once a year, or on a leak (signs everyone out) |
| Hash keys | `AUDIT_HASH_KEY`, `LANGFUSE_USER_HASH_KEY` | yes | `LANGFUSE_USER_HASH_KEY` only: the shell of the retention task (the only Langfuse setting it reads); `AUDIT_HASH_KEY` only a local API run | only on a leak: see "What a hash key change does" below |
| Chat webhook | `ALERT_WEBHOOK_URL` | yes | the shell of the webhook test | every 180 days |

Spec 2: the owner must also check each provider's terms of service for using several accounts [T].

## The order for a list of keys (a save restarts the service)

1. Make the new key at the provider.
2. `uv run python scripts/key_inventory.py` in a window with the current values: note the count
   and fingerprints (the first eight characters of each key's SHA-256; they cannot be turned back).
3. Render, `bfsi-api`, **Environment**: append the new key to the list after a comma. **Save.**
   Render restarts the service (about a minute, during which a request can fail or be slow: do
   it at a quiet time); the old keys still work, so no answer is lost for want of a key.
4. Ask one question. Open `/health`: the provider is `healthy`.
5. Revoke the OLD key at the provider.
6. Remove the old key from the list. **Save.**
7. Run `key_inventory.py` again with the new list: the old fingerprint is gone, the count is back.
   **The script reads only the variables of the window you run it in**, never Render's: type the
   lists into that window first (`Read-Secret`). Its fingerprints prove what YOU typed; that
   Render holds the same is proved by step 4 (a question answered, `/health` healthy).
8. Write a line in the log below. Also update the key on your own machine's shell setup if you use
   it there.
9. **If the key is one of the scheduled tasks' secrets** (`DATABASE_URL`, `EVAL_DATABASE_URL`,
   `QDRANT_URL`, `QDRANT_API_KEY`, `LANGFUSE_USER_HASH_KEY`), replace its encrypted file under
   `bfsi-secrets` too (`docs/DEPLOY.md` 9.1). Otherwise the retention, drift and snapshot tasks
   keep using the revoked value and fail without a sound.

A single-value secret (the Jev key, the database address, the webhook address): replace it, save,
ask one question, then revoke the old one.

## What a hash key change does

Both hash keys are used to turn a name or an address into a hash that cannot be turned back. A new
key gives a new hash for the same input, so:

- **`AUDIT_HASH_KEY`:** (1) the login and registration throttle counters, which are kept under
  hashes of addresses and usernames, **start from zero**: for the next window an attacker who was
  being slowed down gets a fresh allowance. Rotate at a quiet time and, if you are under attack,
  not at all until it is over. (2) The usernames in audit rows are keyed hashes: rows written
  before the change and rows written after it **cannot be matched** to each other. (3) Nothing
  stored in clear changes; no user is signed out.
- **`LANGFUSE_USER_HASH_KEY`:** the hashed user and chat names sent to Langfuse change, so the
  traces of a chat written before the change **can no longer be found by the chat's hash**: the
  deletion of a chat's traces after the change does not reach the older ones. Run the retention
  task once (so queued deletions go out) before you rotate it.
- **`JWT_SECRET_KEY`:** every signed-in user is signed out (their tokens no longer verify).

## A drill (Run 31)

Once, before launch: rotate ONE key you have a spare for, following the order above, and write
down how long it took and what surprised you. Which one: `docs/OWNER_RUNS.md` Run 31 has the
choice (a key of a list such as `GROQ_API_KEYS` is the usual one; `GEMINI_EMBEDDING_KEY_B` is a
safe single value when it is in use: key A is the primary and B only a failover; it is optional,
so it may be empty).

## The log

| Date | What was rotated (setting names only) | Why (routine / leak) | Who | Minutes |
|---|---|---|---|---|
| | | | | |
