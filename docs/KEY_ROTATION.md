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
| Tracing keys | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | yes | the shell of the retention task | every 180 days |
| Signing secret | `JWT_SECRET_KEY` | yes | the shell of a local API run | once a year, or on a leak (signs everyone out) |
| Hash keys | `AUDIT_HASH_KEY`, `LANGFUSE_USER_HASH_KEY` | yes | the shell of the retention task | only on a leak (older hashes stop matching) |
| Chat webhook | `ALERT_WEBHOOK_URL` | yes | the shell of the webhook test | every 180 days |

Spec 2: the owner must also check each provider's terms of service for using several accounts [T].

## The order (no downtime) for a list of keys

1. Make the new key at the provider.
2. `uv run python scripts/key_inventory.py` in a window with the current values: note the count
   and fingerprints (the first eight characters of each key's SHA-256; they cannot be turned back).
3. Render, `bfsi-api`, **Environment**: append the new key to the list after a comma. **Save.**
   Render restarts the service (about a minute); the old keys still work.
4. Ask one question. Open `/health`: the provider is `healthy`.
5. Revoke the OLD key at the provider.
6. Remove the old key from the list. **Save.**
7. Run `key_inventory.py` again with the new list: the old fingerprint is gone, the count is back.
8. Write a line in the log below. Also update the key on your own machine's shell setup if you use
   it there.

A single-value secret (the Jev key, the database address, the webhook address): replace it, save,
ask one question, then revoke the old one.

## A drill (Run 31)

Once, before launch: rotate ONE embedding key (`GEMINI_EMBEDDING_KEY_B`) following the order above
and write down how long it took and what surprised you. It is a safe key to rehearse with: key A
is the primary and B only a failover.

## The log

| Date | What was rotated (setting names only) | Why (routine / leak) | Who | Minutes |
|---|---|---|---|---|
| | | | | |
