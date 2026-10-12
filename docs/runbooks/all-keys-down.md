# Runbook: all keys down (users see "temporarily unavailable")

**When:** Answers say "temporarily unavailable", or the alert `all_keys_exhausted` or `embedding_unavailable` is on the admin banner or in the chat webhook.

**Who does it:** The admin.

## Steps
1. Confirm it. Sign in as admin and open the health page (or `GET /health` with the admin
   token). Note which providers show `circuit_open` or `cooling_down`, and the vector store and
   cache lines.
2. Decide what is down:
   - **One provider** is down: nothing to do. The chain already uses the next one, and the page
     shows the degraded-mode notice. Watch the provider's status page.
   - **Every answer provider** fails: users correctly see "temporarily unavailable". Do NOT switch
     on the offline mock answer in production (`GATEWAY_ENABLE_MOCK` is never set on Render).
3. If the cause is OUR limit (the free tier's daily limit, reset at 00:00 UTC for most providers):
   add another account's key. Render dashboard, `bfsi-api`, **Environment**, edit the provider's
   list (`GROQ_API_KEYS`, `GEMINI_API_KEYS`, `OPENROUTER_API_KEYS`; at most four, comma
   separated), append the new key after a comma, **Save**. Keep the old keys. Render restarts the
   service (about a minute).
4. If both embedding keys fail, add a key to `GEMINI_EMBEDDING_KEY_A` or `_B` the same way.
5. Tell the users it is a known issue (your usual channel) if it lasts more than 30 minutes.

## Check
- `GET /health`: the providers show `healthy` (or only the one that is really down is not).
- Ask one question in the frontend: an answer, not "temporarily unavailable".
- `uv run python scripts/key_inventory.py` on your machine, with the values you set, shows the new
  fingerprint and a count that went up by one.

## Afterwards
Write what happened and the time it took in the release record folder if a setting changed
(`docs/releases/`). If the daily limit was the cause, consider the plan: `docs/COST_PROJECTION.md`.

## Last rehearsal
Never rehearsed. Write the date, who did it and how long it took in `docs/runbooks/README.md`
after each rehearsal.
