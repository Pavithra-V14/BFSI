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
3. If the cause is OUR limit (the free tier's daily limit, which resets at 00:00 UTC for most
   providers [T: check each provider's page]): **do not add another account's key.** The rule
   (`docs/READINESS.md`, terms of service) is one key per provider from one account you own, and
   the pool is not to be used to add up free quota. Options, in this order: wait for the reset;
   switch the provider's plan on that same account (a paid plan has its own limits); tell the
   users it is a known limit. A second key is added only to **replace** a key (an overlap, see
   `docs/KEY_ROTATION.md`), never to add allowance.
4. If both embedding keys fail: `GEMINI_EMBEDDING_KEY_A` and `GEMINI_EMBEDDING_KEY_B` each
   hold **one** key (they are not lists, you cannot append). A is the primary and B the failover,
   each from its own Google account. Put a new key into the one that is empty or limited
   (**Environment**, edit, **Save**); to replace a working one follow `docs/KEY_ROTATION.md`.
5. Tell the users it is a known issue (your usual channel) if it lasts more than 30 minutes.

## Check
- `GET /health`: the providers show `healthy` (or only the one that is really down is not).
- Ask one question in the frontend: an answer, not "temporarily unavailable".
- Optional: `uv run python scripts/key_inventory.py` reads only the variables of YOUR window, so
  type the new values into it first (`Read-Secret`); it then shows the new fingerprint and a
  count that went up by one. It does not read Render.

## Afterwards
Write what happened and the time it took in the release record folder if a setting changed
(`docs/releases/`). If the daily limit was the cause, consider the plan: `docs/COST_PROJECTION.md`.

## Last rehearsal
Never rehearsed. Write the date, who did it and how long it took in `docs/runbooks/README.md`
after each rehearsal.
