# Owner runs: everything you run yourself with real keys and real services

Claude never runs any of these. You run them in your own PowerShell window, in the project
folder, and you send only the short "send back" lines (names, counts and status; never a key, a
URL or a piece of text) to your advisor.

Last updated: 2026-10-12 (after Phase 15, round 3). Each phase that has a real-service check adds its runs
at the end, under "Added by later phases", before the phase can be called closed.

## How to read a run

Every run has the same headings: what it verifies, what must be done before it, what it needs,
the commands, what you should see, what to do if a line differs, what to send back, how to clear
the window, how many real calls it makes and which free-tier limits it may hit, and whether it is
safe to repeat.

## Rules that hold for every run

- One PowerShell window, opened in the project folder (`E:\Claude\BFSI`). Variables set with
  `$env:NAME = ...` last only for that window. Close the window when you are done.
- Keys never go in a chat, a file, a ticket or a log. The commands below read each key with a
  hidden prompt and print nothing back.
- Where a command offers `--load-env`, it reads the names it needs from your `.env` file into
  that one process and prints only the names it loaded. It is off unless you write it. A variable
  already set in the window wins over the file. Use it only if you keep your keys in `.env`.
- After a run, type `$LASTEXITCODE`. `0` is success. Any other number is a failure the run
  explains.
- If a line differs from what is written here, stop, copy the lines the program printed (they
  hold no secrets) and tell your advisor. Do not go on to the next run.

## Common setup (do this once per window)

This function reads a secret with a hidden prompt, so it is never shown on screen and never
lands in your command history:

    function Read-Secret($name) {
      $s = Read-Host "$name (hidden)" -AsSecureString
      $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
      try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
      finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
    }

Then, for each variable a run needs:

    $env:QDRANT_API_KEY = Read-Secret "QDRANT_API_KEY"

A value that is not secret (a name, a list of domains) can be typed with
`$env:COMPANY_NAMES = Read-Host "COMPANY_NAMES"`.

To see that a variable is set without showing it: `[bool]$env:QDRANT_API_KEY` (prints `True`).

To clear one variable: `Remove-Item Env:QDRANT_API_KEY`. To clear everything these runs use:

    "DATABASE_URL","UPSTASH_REDIS_URL","QDRANT_URL","QDRANT_API_KEY","JWT_SECRET_KEY",
    "AUDIT_HASH_KEY","JEV_OPENROUTER_API_KEY","GROQ_API_KEYS","GEMINI_API_KEYS",
    "OPENROUTER_API_KEYS","GEMINI_EMBEDDING_KEY_A","GEMINI_EMBEDDING_KEY_B","COHERE_API_KEY",
    "JINA_API_KEY","LANGFUSE_PUBLIC_KEY","LANGFUSE_SECRET_KEY","LANGFUSE_HOST",
    "LANGFUSE_USER_HASH_KEY","COMPANY_NAMES","COMPANY_DOMAINS" |
      ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }

(Closing the window does the same. The `-ErrorAction` part makes it quiet for names that were
never set.)

## The runs at a glance

| # | Run | Verifies | Status | Order (step, and what comes first) | Needs your explicit yes |
|---|---|---|---|---|---|
| 1 | Apply migrations 0001 to 0003 | Phase 3 | done (date not recorded) | done | no |
| 2 | Create the Qdrant `documents` collection (`--ensure`) | Phase 3 | done (date not recorded) | done | no |
| 3 | Upstash counters and corpus-version check | Phase 3 | done 2026-10-06 | done | no |
| 4 | Make the two keys, apply migration 0004 | Phase 4 | done (date not recorded) | done | no |
| 5 | Create the first admin | Phase 4 | done (date not recorded) | done | no |
| 6 | Run the API locally and check it | Phase 4 | done (date not recorded) | done | no |
| 7 | Real-Qdrant check on a throw-away collection | Phase 5 | done (date not recorded) | done | no |
| 8 | Apply migration 0005 | Phase 7 | pending: owner to confirm | step 1: after nothing (needs Runs 1 to 7) | no (you run it yourself; it may use free-tier calls) |
| 9 | Gateway smoke test with real keys | Phase 2 | pending: owner to confirm | step 2: after Run 8; needs the keys of Run 4 | no (you run it yourself; it may use free-tier calls) |
| 10 | Phase 5 real ingest (steps a to h, and i: the staff-only copy) | Phase 5 | pending: owner to confirm | step 3: after Run 9; Run 8 | no (you run it yourself; it may use free-tier calls) |
| 11 | The IsEmpty filter on real chunks | Phases 3 and 5 | pending: owner to confirm | step 4: after Run 10 | no (you run it yourself; it may use free-tier calls) |
| 12 | Pipeline smoke, one real question per role (12b: the role-isolation test) | Phase 8 | pending: owner to confirm | step 5: after Runs 10 and 11 (12b only after step (i) of Run 10) | no (you run it yourself; it may use free-tier calls) |
| 13 | Pipeline smoke with Langfuse | Phase 8 | pending: owner to confirm | step 6: after Run 12 | no (you run it yourself; it may use free-tier calls) |
| 14 | Real-Upstash check of the cache code (`cache_check.py`) | Phase 9 | pending: owner to confirm | step 7: after Run 8 (a real Upstash address) | no (you run it yourself; it may use free-tier calls) |
| 15 | Refresh sparse vectors (only needed after a tokenizer change) | Phase 9 | pending: owner to confirm | step 8: after only after a tokenizer change: skip otherwise | no (you run it yourself; it may use free-tier calls) |
| 16 | Local end-to-end chat through the API (migrations 0006 and 0007, shadow mode, sessions, feedback) | Phase 10 | pending: owner to confirm | step 9: after Runs 8 and 10 (migrations 0006 to 0008 are applied inside it) | no (you run it yourself; it may use free-tier calls) |
| 17 | Look at every screen of the Streamlit frontend, with the API running | Phase 11 | pending: owner to confirm | step 10: after Run 16 (the API running) | no (you run it yourself; it may use free-tier calls) |
| 18 | Observability: migration 0009, the webhook, the log scan, trace deletion on real Langfuse, the purge and the dashboards | Phase 12 | pending: owner to confirm | step 11: after Run 16 (migration 0009 is applied inside it) | no (you run it yourself; it may use free-tier calls) |
| 19 | Evaluation set-up: the check, the local evaluation database, the test collection `documents_eval` and the three sample documents in it | Phase 13 | pending: owner to confirm | step 13: after Run 10 (the sample documents) and a local evaluation database | no (you run it yourself; it may use free-tier calls) |
| 20 | Evaluation calibration: the judge model, the 30 labelled items, which metrics Jev may decide | Phase 13 | pending: owner to confirm | step 14: after Run 19; needs the evaluation keys | yes: real evaluation keys |
| 21 | The first baseline run and the variance band (the active set, then three runs) | Phase 13 | pending: owner to confirm | step 15: after Run 20 | yes: real evaluation keys |
| 22 | The production sample: the evaluation database in the cloud, the 5 % sample, judging it, the weekly drift report, the purge | Phase 13 | pending: owner to confirm | step 16: after Runs 21 and 18 (migration 0009); the cloud evaluation database | yes: real evaluation keys |
| 23 | The Evaluation page and the "Run eval" button, and the review queue's "add to evaluation set" | Phase 13 | pending: owner to confirm | step 17: after Runs 16 and 19 | no (you run it yourself; it may use free-tier calls) |
| 24 | Secret scan on this machine before a push (the same scan as CI) | Phase 1 (CI), settings added in Phase 13 | pending: owner to confirm | step 12: after nothing; repeat before every push | no (you run it yourself; it may use free-tier calls) |
| 25 | Record the provider cassettes: five real calls, one per provider (Cohere, Jina, Jev, Gemini embeddings, OpenRouter) | Phase 14 | pending: owner to confirm | step 18: after nothing else; needs five provider keys | yes: real provider keys (5 calls) |
| 26 | Stubbed-model load test on this machine (users step up from 10; no key) | Phase 14 | pending: owner to confirm | step 19: after nothing (no key, no cloud address) | no (you run it yourself; it may use free-tier calls) |
| 27 | Real-provider load test, short, on this machine: local SQLite, test collection read only, estimate and a single-use ticket first | Phase 14 | pending: owner to confirm | step 20: after Runs 26 and 16; the `documents_eval` collection of Run 19 | yes: real provider keys, free-tier quota |
| 28 | First deploy to Render (DEPLOY.md section 2): services, client address check, memory check, shadow stage | Phase 15 | pending: owner to confirm | step 26: after Runs 1 to 3, 10 and 24 (migrations 0004 to 0010 are applied by DEPLOY.md 2.1) | yes: real keys and accounts, a public service |
| 29 | The rollback drill: back to the last good build in under 5 minutes, with a stopwatch | Phase 15 | pending: owner to confirm | step 27: after Run 28 | no (you run it yourself; it may use free-tier calls) |
| 30 | Qdrant snapshot, download, and a restore of the download into a throw-away collection (`documents_restore_test`); the re-ingest from originals/ is the tested route | Phase 15 | pending: owner to confirm | step 28: after Runs 2 and 10 | no (you run it yourself; it may use free-tier calls) |
| 31 | A key rotation drill on one key you have a spare for | Phase 15 | pending: owner to confirm | step 29: after Run 28 | no (you run it yourself; it may use free-tier calls) |
| 32 | The database connection count on the deployed service, against the plan's limit | Phase 15 | pending: owner to confirm | step 30: after Run 28 | no (you run it yourself; it may use free-tier calls) |
| 33 | The scheduled tasks on this machine: retention, drift, snapshot (Windows Task Scheduler) | Phase 15 | pending: owner to confirm | step 31: after Runs 22 and 30; the encrypted files of DEPLOY.md 9.1 | no (you run it yourself; it may use free-tier calls) |
| 34 | Migration 0010 and the withdrawn-document record: a delete in the test collection records the id, moves the PDF and a restore finds nothing left | Phase 15 | pending: owner to confirm | step 21: after Run 19; migration 0010 on the local file | yes: it changes `documents_eval` |
| 35 | The red-team set: development half, then the held-out half (its own estimate and code each) | Phase 16 | pending: owner to confirm | step 22: after Runs 19, 20 and 21 | yes: real evaluation keys |
| 36 | The security scan: pip-audit of all five requirements files, the bandit baseline, gitleaks | Phase 16 | pending: owner to confirm | step 23: after nothing | no (you run it yourself; it may use free-tier calls) |
| 37 | Numeric targets from the baseline: `app.eval targets`, then the absolute floors you sign | Phase 16 | pending: owner to confirm | step 24: after Run 21 | no (you run it yourself; it may use free-tier calls) |
| 38 | The cost projection against a dry run | Phase 16 | pending: owner to confirm | step 25: after Run 21 or Run 27 | no (you run it yourself; it may use free-tier calls) |
| 39 | The pre-launch gate and the readiness score, with the second review | Phase 16 | pending: owner to confirm | step 32: after Runs 35 to 38 (and 21, 26, 29 as far as done) | no (you run it yourself; it may use free-tier calls) |
| 40 | One conversation per chat (the chat keeps its id even when a save is late) | Change round 1, step 1 | pending: owner to confirm | after Run 16 | no (you run it yourself; it may use free-tier calls) |
| 41 | The key panel: migration 0011, calls today and calls left per key | Change round 1, step 2 | pending: owner to confirm | after Run 40 | no (you run it yourself; it may use free-tier calls) |
| 42 | Alert reasons on the Alerts screen | Change round 1, step 3 | pending: owner to confirm | after Run 41 | no (you run it yourself; it may use free-tier calls) |
| 43 | The Delete button of the Documents screen, on a throw-away document | Change round 1, step 4 | pending: owner to confirm | after Run 42 and Run 8 to 12 | no (you run it yourself; it may use free-tier calls) |
| 44 | The purge of all data: a dry run, then (only when you mean it) the real one | Change round 1, step 6 | pending: owner to confirm | after Run 43 | no (you run it yourself; the real purge cannot be undone) |
| 45 | The Dark mode switch: look at both themes on every screen | Change round 1, step 7 | pending: owner to confirm | after Run 17 | no (you run it yourself) |

How to read the table. **Status** is "done" with the date, or "pending: owner to confirm": until
you tell me which runs you have done, every run from 8 onward is shown as pending and nothing is
guessed. **Order** is the step number in the sequence the runs can be done in, and what has to come
first (the "Before it" line of the run has the detail). Several runs can be done in either order
where nothing is listed between them. **Needs your explicit yes** is "yes" for a run that spends
real provider quota in a way that must be agreed first (an evaluation, a load or a deploy), or that
changes a test collection; the others you run yourself, in your own window, and Claude never runs
any of them.

**Pending migrations, in order:** 0005 (Run 8), 0006 to 0008 (Run 16), 0009 (Run 18) and **0010
`withdrawn_documents` (Phase 15 round 3)**. The runner applies every pending file in order, so
`docs/DEPLOY.md` 2.1 applies whatever is still pending on the cloud database, 0010 included.
The local SQLite file of the test collection gets 0010 in Run 34 (or from any
`uv run python -m app.db.migrate` with `DATABASE_URL` set to that file). Until 0010 is applied,
`ingest` and `delete` fail with `no such table: withdrawn_documents`.

Order matters for the pending runs: 8, then 9, then 10, then 11, then 12, then 13. Runs 9 and 10
use up free-tier calls, so do them on a day when you have not used the providers much. The
evaluation runs go 19, 20, 21, then 22 and 23; 20, 21 and 22 use the EVALUATION keys (not the
production ones) and each one shows an estimate first and starts only with your typed code.

---

# A. Already done

These are kept so that you can repeat them on a fresh database or a fresh Qdrant, and so that
the record is in one place. The dates are the ones I have; where a date is blank, you told me it
was done and I did not note the day: please write it in.

## Run 1. Apply migrations 0001 to 0003: DONE (date: ______)

- **Verifies:** Phase 3 (the schema: users, sessions, messages, audit log, allow-list, feedback,
  review queue).
- **Before it:** a Neon project and its direct (non-pooled) connection string.
- **Needs:** `DATABASE_URL` (the direct URL, ending `?sslmode=require`).
- **Commands:**

      $env:DATABASE_URL = Read-Secret "DATABASE_URL"
      uv run python -m app.db.migrate --dry-run
      uv run python -m app.db.migrate
      uv run python -m app.db.migrate --dry-run

- **You should see:** the first dry run lists `pending 0001_...sql` and the others, one per line;
  the real run prints `applied 0001_...sql` and so on; the last dry run prints
  `nothing to apply`.
- **If it differs:** `error: the environment variable DATABASE_URL is not set` means the
  variable is not set in this window. `migration failed (OperationalError)` means the URL, the
  network or `sslmode` is wrong. `was changed after it was applied` means a file was edited:
  stop and tell your advisor.
- **Send back:** the list of `applied` / `pending` file names and the last line.
- **Clear:** `Remove-Item Env:DATABASE_URL`.
- **Calls:** one database connection, no model calls, no free-tier limit.
- **Safe to repeat:** yes. Applied files are skipped.

## Run 2. Create the Qdrant `documents` collection: DONE (date: ______)

- **Verifies:** Phase 3 (collection with dense and sparse vectors and the payload indexes).
- **Before it:** a Qdrant Cloud cluster.
- **Needs:** `QDRANT_URL`, `QDRANT_API_KEY`.
- **Commands:**

      $env:QDRANT_URL = Read-Secret "QDRANT_URL"
      $env:QDRANT_API_KEY = Read-Secret "QDRANT_API_KEY"
      uv run python -m app.vector.documents --ensure
      uv run python -m app.vector.documents --ensure

- **You should see:** the first time `collection 'documents': created` and a list after
  `payload indexes created:`; the second time `collection 'documents': already exists` and
  `payload indexes created: none`.
- **If it differs:** a `settings` or `credentials` error names the variable to fix; `could not
  be reached` is the URL or the network.
- **Send back:** the two result lines.
- **Clear:** `Remove-Item Env:QDRANT_URL, Env:QDRANT_API_KEY`.
- **Calls:** a handful of Qdrant management calls; no model calls.
- **Safe to repeat:** yes.

## Run 3. Upstash counters and corpus-version check: DONE 2026-10-06

- **Verifies:** Phase 3 (the cache store's counters, and that the corpus version is seeded and
  bumped in one transaction on real Upstash).
- **What you saw:** counters went 1 then 2, a read gave 2, the corpus version was seeded in
  milliseconds, a bump added 1, and no alert events were raised.
- **Note:** this was a one-off check; there is no script for it in the repository, so there is
  no command to repeat here. If it is ever needed again, ask for an owner-run script (Phase 9
  changes the cache and will want a re-check).
- **Needs:** `UPSTASH_REDIS_URL`. **Calls:** a few Redis commands. **Safe to repeat:** yes.

## Run 4. Make the two keys and apply migration 0004: DONE (date: ______)

- **Verifies:** Phase 4 (login tokens and audit hashing need two different random keys; 0004
  adds `users.is_active`).
- **Before it:** Run 1.
- **Commands:**

      $env:JWT_SECRET_KEY = (uv run python -c "import secrets; print(secrets.token_urlsafe(32))")
      $env:AUDIT_HASH_KEY = (uv run python -c "import secrets; print(secrets.token_urlsafe(32))")
      $env:DATABASE_URL = Read-Secret "DATABASE_URL"
      uv run python -m app.db.migrate --dry-run
      uv run python -m app.db.migrate
      uv run python -m app.db.migrate --dry-run

- **You should see:** the first dry run `pending 0004_users_active.sql` (and later files if you
  have not applied them), then `applied ...`, then `nothing to apply`.
- **Keep:** both keys in your password manager. Render needs the same two values later. If you
  change `AUDIT_HASH_KEY`, old audit rows can no longer be matched to usernames.
- **Send back:** the `applied` lines only. Never the keys.
- **Clear:** `Remove-Item Env:JWT_SECRET_KEY, Env:AUDIT_HASH_KEY, Env:DATABASE_URL`.
- **Calls:** none to models. **Safe to repeat:** yes for the migration; do not make new keys
  unless you mean to replace the old ones.

## Run 5. Create the first admin: DONE (date: ______)

- **Verifies:** Phase 4 (only this command can make an admin; the API cannot).
- **Before it:** Run 4.
- **Commands:**

      $env:DATABASE_URL = Read-Secret "DATABASE_URL"
      uv run python -m app.accounts.admin_cli create-admin --username NAME --email ADDRESS

  It asks for the password twice with a hidden prompt.
- **You should see:** `created admin 'NAME' (id <uuid>)`.
- **If it differs:** `that username or email is already in use` (already created);
  `that password is not allowed: ...` lists the rules to meet.
- **Send back:** the word `created`. Not the username, the email or the id.
- **Clear:** `Remove-Item Env:DATABASE_URL`.
- **Safe to repeat:** it refuses a duplicate. `set-active --username NAME --active no` turns an
  account off.

## Run 6. Run the API locally and check it: DONE (date: ______)

- **Verifies:** Phase 4 (the app starts, refuses weak keys, `/live` answers).
- **Before it:** Runs 4 and 5.
- **Commands (window A):**

      $env:DATABASE_URL = Read-Secret "DATABASE_URL"
      $env:UPSTASH_REDIS_URL = Read-Secret "UPSTASH_REDIS_URL"
      $env:JWT_SECRET_KEY = Read-Secret "JWT_SECRET_KEY"
      $env:AUDIT_HASH_KEY = Read-Secret "AUDIT_HASH_KEY"
      uv run uvicorn app.api.main:create_app_from_env --factory

  Then in a second window: `Invoke-RestMethod http://127.0.0.1:8000/live`.
- **You should see:** uvicorn says `Uvicorn running on http://127.0.0.1:8000`; the second window
  prints `status : ok`. Stop with Ctrl+C.
- **If it differs:** it refuses to start with a message naming the variable (missing, short or
  placeholder).
- **Send back:** `live: ok`.
- **Clear:** the four variables. **Calls:** none to models. **Safe to repeat:** yes.

## Run 7. Real-Qdrant check on a throw-away collection: DONE (date: ______)

- **Verifies:** Phase 5 (a DATETIME payload index works with the "active only" filter; the
  hybrid query takes the filter on both branches).
- **Before it:** Run 2.
- **Commands:**

      $env:QDRANT_URL = Read-Secret "QDRANT_URL"
      $env:QDRANT_API_KEY = Read-Secret "QDRANT_API_KEY"
      uv run python scripts/qdrant_check.py --run

- **You should see**, one per line:
  `using a throw-away collection named check_<hex>`, then `collection created PASS`,
  `sparse vector uses IDF PASS`, `payload indexes present PASS`,
  `IsEmpty keeps null/missing/[] PASS`, `hybrid RRF, filter on both PASS`,
  `throw-away collection deleted DONE`, and `all checks passed`. Exit code 0.
- **If it differs:** any `FAIL` line: send it back. If `deleted FAILED`, remove the collection
  named `check_<hex>` in the Qdrant dashboard by hand. It never touches `documents`.
- **Send back:** the eight lines.
- **Clear:** the two variables. **Calls:** about ten Qdrant calls, no model calls.
- **Safe to repeat:** yes.

---

# B. Pending, in the order to run them

## Run 8. Apply migration 0005: PENDING

(It stays PENDING until you tell me it is applied; then I will mark it done with the date you
give: ______.)

- **Verifies:** Phase 7 (adds `messages.history_excluded`: a blocked, reworded or refused
  question must never feed the history).
- **Before it:** Run 4 (0001 to 0004 applied).
- **Needs:** `DATABASE_URL`.
- **Commands:**

      $env:DATABASE_URL = Read-Secret "DATABASE_URL"
      uv run python -m app.db.migrate --dry-run
      uv run python -m app.db.migrate
      uv run python -m app.db.migrate --dry-run

- **You should see:** `pending 0005_messages_history_excluded.sql` (only that file if the others
  are applied), then `applied 0005_messages_history_excluded.sql`, then `nothing to apply`.
- **If it differs:** more than one `pending` file means an earlier migration was not applied:
  let it apply them in order, then send the list. `was changed after it was applied`: stop.
- **Send back:** the `applied` line(s) and `nothing to apply`.
- **Clear:** `Remove-Item Env:DATABASE_URL`.
- **Calls:** one database connection. **Safe to repeat:** yes.

## Run 9. Gateway smoke test with real keys

- **Verifies:** Phase 2 (every provider key works through the gateway: Jev, Gemini embedding,
  Gemini, Groq, OpenRouter). It also proves that each key is read from the right variable.
- **Before it:** nothing else. Do it first among the pending runs: it is the cheapest proof that
  the keys are right, and Runs 10 to 13 depend on them.
- **Needs (names only):** `JEV_OPENROUTER_API_KEY` (needs OpenRouter credit: Jev is a paid
  model), `GEMINI_EMBEDDING_KEY_A`, `GEMINI_API_KEYS`, `GROQ_API_KEYS`, `OPENROUTER_API_KEYS`.
  Cohere and Jina keys are not used by this script (see the note below).
- **Commands:**

      $env:JEV_OPENROUTER_API_KEY = Read-Secret "JEV_OPENROUTER_API_KEY"
      $env:GEMINI_EMBEDDING_KEY_A = Read-Secret "GEMINI_EMBEDDING_KEY_A"
      $env:GEMINI_API_KEYS = Read-Secret "GEMINI_API_KEYS"
      $env:GROQ_API_KEYS = Read-Secret "GROQ_API_KEYS"
      $env:OPENROUTER_API_KEYS = Read-Secret "OPENROUTER_API_KEYS"
      uv run python scripts/gateway_smoke.py --run

  (A list of keys is comma-separated in one variable.) Or add the keys to `.env` and run the
  script without setting anything: it reads the gateway names from `.env` by itself.
- **You should see**, one line per call (names and numbers only):
  - a first line `loaded N variable(s) from .env: ...` or `no .env file found: using the
    variables already set in this shell`;
  - `jev decision  OK  decider=jev model=... snapshot=... probability=0.xx input_tokens=...
    output_tokens=... cost=... calibrated=...`;
  - `gemini embedding  OK  model=... length=768 expected=768 norm=<a number> key=<fingerprint>`
    (the length must equal `expected`);
  - `gemini text call  OK ... text_non_empty=True finish_reason=stop ...`, and the same for
    `groq text call` and `openrouter text call`;
  - last line `done: all calls worked`. Exit code 0.
- **If a line differs:**
  - `SKIPPED reason=... is not set`: that key is not in the window; set it and run again.
  - `jev decision FELL BACK decider=... reason=...`: Jev did not answer: most often no OpenRouter
    credit, or the key is wrong. Send the reason word.
  - `FAILED reason=rate_limited`: a free-tier limit; wait a few minutes and run again.
  - `FAILED reason=auth` or `credentials`: the key is wrong or revoked for that provider.
  - `EMPTY`: the call worked but the text was empty (a model that spends its limit thinking):
    send the `finish_reason` and `reasoning_tokens` numbers.
- **Send back:** each line's name, its status word and the numbers. Never a key (the
  fingerprint is safe: it is a short hash).
- **Clear:** the five variables (see the clearing list above).
- **Real calls:** about 5: 1 Jev decision, 1 embedding, 3 tiny text calls. Free tiers: Gemini
  and Groq free limits are per minute and per day; five calls will not reach them. The Jev call
  costs a fraction of a cent of OpenRouter credit.
- **Safe to repeat:** yes.
- **Note on Cohere and Jina:** this script has no rerank call. They are exercised by Run 12
  (the `rerank` line shows which one answered). To test Jina on its own, unset
  `COHERE_API_KEY` in the window for one run of Run 12 with Jina's key set. If you want a
  dedicated rerank line in this script, ask for it in a later phase: I have not added it.

## Run 10. Phase 5 real ingest (the real-run procedure, steps a to h)

- **Verifies:** Phase 5 (parsing, chunking, embeddings, image captions, entity extraction,
  allow-list, the true no-op re-run, delete, failed page and retry). It also verifies, for the
  first time on real services: a real embedding call, a real Gemini caption, the allow-list /
  review-queue SQL on real Postgres, and the "unchanged run changes nothing" rule.
- **Before it:** Runs 2, 7 and 8 (collection, Qdrant check, all migrations), and Run 9 (keys
  work). Install once: poppler and tesseract on your `PATH` (`pdftoppm -h` and
  `tesseract --version` must print something), and `uv pip install -r requirements-ingest.txt`
  (about 400 MB for the language model).
- **Needs (names only):** `DATABASE_URL`, `QDRANT_URL`, `QDRANT_API_KEY`, `UPSTASH_REDIS_URL`,
  `GEMINI_EMBEDDING_KEY_A` (and `_B` if you have it), `GEMINI_API_KEYS`. Optional:
  `GROQ_API_KEYS`, `OPENROUTER_API_KEYS`, `JEV_OPENROUTER_API_KEY`, `COMPANY_NAMES`,
  `COMPANY_DOMAINS`.
- **Set them:**

      $env:DATABASE_URL = Read-Secret "DATABASE_URL"
      $env:QDRANT_URL = Read-Secret "QDRANT_URL"
      $env:QDRANT_API_KEY = Read-Secret "QDRANT_API_KEY"
      $env:UPSTASH_REDIS_URL = Read-Secret "UPSTASH_REDIS_URL"
      $env:GEMINI_EMBEDDING_KEY_A = Read-Secret "GEMINI_EMBEDDING_KEY_A"
      $env:GEMINI_API_KEYS = Read-Secret "GEMINI_API_KEYS"
      $env:GROQ_API_KEYS = Read-Secret "GROQ_API_KEYS"
      $env:OPENROUTER_API_KEYS = Read-Secret "OPENROUTER_API_KEYS"
      $env:JEV_OPENROUTER_API_KEY = Read-Secret "JEV_OPENROUTER_API_KEY"

- **A preflight (Phase 9):** `ingest`, `delete`, `review` and `refresh-sparse` first check that
  Upstash accepts the commands a cache update needs. If it does not, they stop at once with
  `error: cache unavailable, nothing was changed; ...` and exit code 1. Nothing was written, no
  model call was paid for: check `UPSTASH_REDIS_URL` and the Upstash dashboard (the daily request
  quota too: the free tier allows 10,000 commands a day), then run the same command again. If a
  run instead ends with `ingest incomplete`, Upstash failed after the check: run the same command
  again, and until you do an answer that cited the document can still be served.
- **Rules that always hold:** use only the three sample documents (a customer-specific document
  is refused: its text would go to a model provider). `--doc-id` is a short name you choose and
  keep. Every command is safe to run again: a run that stops never leaves a half-activated
  document. After any command check `$LASTEXITCODE`: `0` done; `1` an error or "ingest
  incomplete" (a cache step failed: run the same command again); `2` a page failed so the
  document was not activated; `130` you pressed Ctrl+C.

### (a) Check that the settings are seen

    uv run python -m app.ingest status --doc-id insurance-flexi-property

- **You should see:** `document: insurance-flexi-property`,
  `chunks: active 0, held back 0, rejected 0, inactive 0 (stored 0)`, `document version: 0`,
  `unfinished cache step (pending-finish marker): no`, `allow-list entries: none`,
  `failed pages waiting in the review queue: 0`. Exit code 0.
- **If it differs:** `error: these settings are not set: ...` names the missing variables.
  `a store could not be reached`: check the URL and your network.

### (b) One small document first

Count first (nothing is called):

    uv run python -m app.ingest ingest spec/samples/company/insurance_flexi_commercial_property.pdf `
      --doc-id insurance-flexi-property --doc-type policy --access-role customer `
      --product-line insurance --title "Flexi Commercial Property Policy" --dry-run

- **You should see about:** `pages parsed: 3 of 3`, `running headers and footers removed: 12`
  followed by 12 `removed, page N: ...` lines, `chunks: 18 (text 11, table 7); images: 1`,
  `embedding calls 2`, `image caption calls: 1; table summary calls: 7`,
  `clause label calls: 19`, `entity questions: 11 of 55 candidates`, and
  `no service was called (dry run)`.
- **If it differs:** if a `removed` line is real text, stop and tell your advisor. If `pages
  parsed` is lower than the page count, a page failed to parse (see (h)). A `warning: page N: a
  table lost K word(s)` means a table's cells are not complete: tell your advisor.

Then the same command without `--dry-run` (several minutes: about a minute to read the PDF,
then the model calls):

    uv run python -m app.ingest ingest spec/samples/company/insurance_flexi_commercial_property.pdf `
      --doc-id insurance-flexi-property --doc-type policy --access-role customer `
      --product-line insurance --title "Flexi Commercial Property Policy"

- **You should see:** a `note:` line, then
  `insurance-flexi-property: new 19, changed 0, unchanged 0, removed 0, version 1`. Exit 0.
- **If it differs:** `ingest incomplete` (exit 1): run the same command again (it says
  `finished the cache steps an earlier run had left undone`). `the model providers could not
  answer` (exit 1): a free-tier limit or a key problem: wait a few minutes and run it again.
  `NOT activated ... page N failed` (exit 2): see (h).

### (c) The same ingest again: it must be a true no-op

Run exactly the same command again.

- **You should see:** `insurance-flexi-property: new 0, changed 0, unchanged 19, removed 0,
  version 1` and `nothing changed: no write, no version bump, no audit row`. No model call.
- **If it differs:** if `new` or `changed` is not 0 the document is not recognised as the same:
  stop and tell your advisor. Do not go on to (d).

### (d) Status

    uv run python -m app.ingest status --doc-id insurance-flexi-property

- **You should see:** `chunks: active 19, held back 0, rejected 0, inactive 0 (stored 19)`,
  `document version: 1`, `unfinished cache step (pending-finish marker): no`,
  `sparse vectors out of date: 0 of 19` (Phase 9: every chunk was made by the current
  tokenizer), `allow-list entries: approved N, pending M` (about 70 to 110 in all, most
  approved by themselves), `failed pages waiting in the review queue: 0`.
- **If `sparse vectors out of date` is above 0** on a fresh ingest: send the line. After a
  tokenizer change it is expected: see Run 15.
- **If it differs:** a marker of `YES` means an earlier run did not finish its cache steps: run
  the `ingest` command again and it clears.

### (e) Review

    uv run python -m app.ingest review chunks --doc-id insurance-flexi-property
    uv run python -m app.ingest review entities --doc-id insurance-flexi-property

- **You should see:** `no chunk is held back for this document` (the samples contain no
  instruction-like text; if one is held back it shows the page, the clause and why: read it,
  then `a` approve, `r` reject, `s` skip, `q` quit). The entities command shows the names and
  codes the program was not sure about, each with its sentence: `a` approves the group (full
  name, "Mr. Sharma", "R. Sharma"), the surname alone needs `t`, `r` rejects, `s` skips, `A`
  approves all the rest, `q` quits. Run `status` again: the `pending` count is lower.
- **If it differs:** both commands stop with a clear message if an ingest is running on the
  same document; run them again when it ends.

### (f) The delete test

    uv run python -m app.ingest delete --doc-id rehearsal-insurance-1

It asks you to type the id (see the note below: the id is a throw-away one).

- **A delete records the id as withdrawn for good** (so that a restore can never bring it
  back), so this drill uses a **throw-away id**, never a real one. Ingest the sample again under
  `rehearsal-insurance-1` (the command of (b) with `--doc-id rehearsal-insurance-1`; quick: the
  answers are cached), then delete that id (the command above with `--doc-id
  rehearsal-insurance-1`).
- **You should see:** `rehearsal-insurance-1: removed 19 chunk(s), N allow-list entr(ies); 0
  chat message(s) flagged 'source removed'`, then `recorded as withdrawn (reason: other); 1
  original PDF(s) moved`. Then `status --doc-id rehearsal-insurance-1` shows `stored 0`,
  `allow-list entries: none`, marker `no`. Next time use `rehearsal-insurance-2`.

### (g) The other two documents

    uv run python -m app.ingest ingest spec/samples/company/banking_rtl_standard_facility_agreement.pdf `
      --doc-id banking-rtl-facility --doc-type policy --access-role customer --product-line banking
    uv run python -m app.ingest ingest spec/samples/company/finserv_mutual_fund_ecas_and_servicing.pdf `
      --doc-id finserv-mf-ecas --doc-type policy --access-role customer --product-line finserv

- **You should see:** `new 22 ...` for the banking one and `new 21 ...` for the finserv one.
  Run (c) and (d) for each (substitute the `--doc-id`).
- **If it differs:** a provider says no: it stops with a clear message. That is safe: run the
  same command again; the calls that already worked are not repeated.

### (h) A failed page and its retry (use the banking document)

1. Use a **throw-away id**: every command below takes `--doc-id rehearsal-banking-1` instead
   of `banking-rtl-facility` (next time `rehearsal-banking-2`), because a delete records an id as
   withdrawn for good. There is nothing to delete first.
2. `Remove-Item -Recurse -Force originals\.cache` (so the picture is asked for again).
3. Break the Gemini key for this window only: `$env:GEMINI_API_KEYS = "not-a-real-key"`
4. Run the banking ingest command with `--doc-id rehearsal-banking-1`.
   - **You should see:** `document 'rehearsal-banking-1' was NOT activated; the old version
     (if any) stays active`, one or more `page N failed (<an error name>)` (at least page 2,
     which holds the picture), and `each failed page is in the review queue; run the same
     command again to retry`. Exit code 2.
5. `status`: `stored 0` and `failed pages waiting in the review queue:` as many as there were
   `page N failed` lines.
6. Put the real key back (`$env:GEMINI_API_KEYS = Read-Secret "GEMINI_API_KEYS"`), run the same
   ingest again: `new 22 ...`, exit 0, and `status` shows `failed pages waiting ... : 0`.
7. Remove the throw-away copy, which holds the same text as the real banking document:
   `uv run python -m app.ingest delete --doc-id rehearsal-banking-1` (type its id).

### (i) The staff-only copy, for the role-isolation test (Run 12b uses it)

A second copy of the eCAS sample, stored under another id and visible to staff only. Do this
after (g), with the same variables. It adds a document; it does not change the others.

    uv run python -m app.ingest ingest spec/samples/company/finserv_mutual_fund_ecas_and_servicing.pdf `
      --doc-id ecas-staff-only --doc-type policy --access-role staff --product-line finserv

- **You should see:** the same lines as the finserv ingest in (g), with the new id:
  `ecas-staff-only: new 21, changed 0, unchanged 0, removed 0, version 1`. Exit code 0. It is
  quick and makes few model calls (the captions, summaries and labels are in `originals/.cache`
  from (g)).
- **Then:** `uv run python -m app.ingest status --doc-id ecas-staff-only` shows
  `chunks: active 21, held back 0, rejected 0, inactive 0 (stored 21)`.
- **If it differs:** `new` is not 21: send the line. A provider limit: run the command again.
- **Do not delete it yet:** Run 12b needs it, and deletes it itself.

- **If a step differs:** stop and copy the lines printed (they hold no secrets).
- **Send back (whole run):** for each document the `new/changed/unchanged/removed/version` line,
  the `status` lines (counts only), the exit codes, and anything that differed. Never document
  text.
- **Clear:** all nine variables (the clearing list above). `originals/` holds the ONLY copy of
  your PDFs: back it up yourself.
- **Real calls (all three documents, from the dry runs):** about 6 embedding calls (16 texts
  each), 3 image captions (Gemini), 22 table summaries, 62 clause labels and 29 entity
  questions: roughly 120 model calls, spread over Gemini, Groq, OpenRouter and Jev. **Free
  tiers:** Gemini's limits are unpublished and may pause the run; Groq allows about 30 requests
  a minute. The program waits for room and stops with a clear message if a provider still says
  no. Do (h) last: it makes extra calls.
- **Safe to repeat:** yes, every step.

## Run 11. The IsEmpty filter on real chunks

- **Verifies:** Phases 3 and 5 (the "active only" filter, which treats a missing, null or empty
  `effective_to` as active and any value as inactive, agrees with the program's own
  `chunk_is_active` on real chunks of the real `documents` collection). Run 7 proved it on
  made-up points; this proves it on your real ones.
- **Before it:** Run 10 (the three documents ingested).
- **Needs:** `DATABASE_URL`, `QDRANT_URL`, `QDRANT_API_KEY`, `UPSTASH_REDIS_URL` (as in Run 10).
- **How:** there is no separate script. The check is made of two readings of the same real
  chunks that must agree. `status` counts each stored chunk with the program's own
  `chunk_is_active` (in the program); Run 12 reads the same collection through the Qdrant
  `IsEmpty` filter (in Qdrant). If they disagree, Run 12 returns no chunk although `status`
  says `active` is above 0, or it returns a chunk that `status` calls inactive.
- **Commands:**

      uv run python -m app.ingest status --doc-id insurance-flexi-property
      uv run python -m app.ingest status --doc-id banking-rtl-facility
      uv run python -m app.ingest status --doc-id finserv-mf-ecas
      uv run python -m app.ingest status --doc-id ecas-staff-only

- **You should see:** for each: `active` equals `stored` (19, 22, 21 and 21 for the three
  samples and the staff-only copy) and `held back 0, rejected 0, inactive 0`.
- **The second half is in Run 12:** each real question must return `citations=` of at least 1,
  and the `retrieve` line must show candidates above 0. If `status` says `active 19` and Run 12
  returns nothing for a question the document answers, the filter and the program disagree:
  send both outputs.
- **If it differs:** `inactive` above 0 on a fresh ingest means chunks were stored with a
  non-empty `effective_to`: send the `status` lines.
- **Send back:** the three `chunks:` lines.
- **Clear:** the variables. **Calls:** Qdrant reads only. **Safe to repeat:** yes.
- **Limit of this check:** it does not flip one real chunk to inactive and watch it disappear
  from search (that would change your real collection). If you want that, ask for a dedicated
  owner-run script; I have not built one.

## Run 12. Pipeline smoke: one real question per role

- **Verifies:** Phase 8 (the whole 17-stage pipeline on your real Qdrant and real models: Jev
  triage, embedding, hybrid retrieval, rerank by Cohere or Jina, generation, the output guard).
  This is also the first real check of role mapping, the 6 s generate minimum and the model
  deciders under the new time limits.
- **Before it:** Runs 8, 9, 10 and 11. The masker (the name model) loads first: about a minute
  on a small machine; the script prints how long.
- **Needs (names only):** `QDRANT_URL`, `QDRANT_API_KEY`, `JEV_OPENROUTER_API_KEY`,
  `GEMINI_EMBEDDING_KEY_A`, `GEMINI_API_KEYS`, `GROQ_API_KEYS`, `OPENROUTER_API_KEYS`,
  `COHERE_API_KEY`, `JINA_API_KEY`. Not needed: `DATABASE_URL` (a throw-away SQLite file is
  used and deleted; your real database is never opened) and `UPSTASH_REDIS_URL` (no cache
  without `--with-cache`).
- **Commands:** set the nine variables with `Read-Secret` as in Run 10, plus
  `$env:COHERE_API_KEY = Read-Secret "COHERE_API_KEY"` and
  `$env:JINA_API_KEY = Read-Secret "JINA_API_KEY"`. Add `--load-env` to each command instead if
  you keep your keys in `.env`.

      uv run python scripts/pipeline_smoke.py --run --role customer --question "What deductible applies to storm and flood claims?"
      uv run python scripts/pipeline_smoke.py --run --role staff --question "What are the penal charges for non-compliance on the facility?"
      uv run python scripts/pipeline_smoke.py --run --role admin --question "Which folio is pending nomination?"

  (The three questions match the three sample documents. All three are ingested with
  `--access-role customer`, so every role sees the same documents: this run proves the role
  mapping runs and the roles do not break retrieval. It does not show a staff-only document
  being hidden from a customer: that needs a staff-only document, which the samples do not
  include.)
- **You should see**, for each command, one line per stage in this order:

      masker                OK        seconds=NN.N
      sparse vectors out of date: 0 of NN
      authenticate         SKIPPED  ...
      local_scan           OK        ms=..
      triage               OK        ms=.. decider=jev model=... provider=... key=<fp> tokens_in=.. tokens_out=..
      injection_decision   OK        ...
      rewrite              SKIPPED  ...
      domain_gate          OK        ...
      cache_lookup         OK        ...
      embed                OK        ... model=... key=<fp> tokens_in=..
      retrieve             OK        ...
      rerank               OK        ... model=... key=<fp> tokens_in=..
      context_guard        OK        ...
      confidence           OK        ...
      generate             OK        ... decider=... model=... provider=... key=<fp> tokens_in=.. tokens_out=..
      output_guard         OK        ...
      cache_write          SKIPPED  ...
      persist              OK        ...
      respond              OK        ...
      RESULT               ANSWER    reason=None citations=N warnings=0 degraded=None cached=False confidence=None tokens=NNN seconds=NN.N
      cited doc ids: <the id of the sample document, for example insurance-flexi-property>
      review rows: 0
      done: the pipeline answered

  `cited doc ids` must name the document the question is about (insurance question:
  `insurance-flexi-property`; banking: `banking-rtl-facility`; eCAS: `finserv-mf-ecas`), never
  text. `review rows: 0` for an answer. `authenticate` is SKIPPED because the smoke run has no rate limiter; `rewrite` is SKIPPED
  because a first message that is clear needs no rewrite; `cache_write` is SKIPPED because the
  run has no cache. Exit code 0 means an answer. The question and the answer text are never
  printed.
- **If a line differs:**
  - `RESULT REFUSAL reason=no_context`: nothing above the relevance floor: the document is not
    ingested (Run 10) or the question does not match it. Try another of the three questions.
  - `RESULT UNAVAILABLE reason=...`: read the stage line with `ERROR`; the `error=` word names
    it: `embedding_unavailable` (embedding key), `vector_store_unavailable` (Qdrant),
    `providers_unavailable` (answer providers), `no_time` (a minimum was not met).
  - `degraded=lexical_rerank` on the RESULT line: neither Cohere nor Jina answered. Send the
    `rerank` line.
  - `warnings=` above 0 or `confidence=low`: the answer was labelled; send the numbers.
  - `masker FAILED`: the name model did not load in 240 s: run again.
- **Send back (per role):** the stage names and statuses, the `decider=`/`model=`/`provider=`
  words, the `key=` fingerprints, the token counts, the RESULT line, and the exit code. No
  question, no answer, no key.
- **Clear:** all variables.
- **Real calls (per question):** about 4: 1 Jev decision, 1 embedding, 1 rerank, 1 answer
  (plus 1 rewrite if the question needs one). Three roles: about 12. Free tiers: Cohere's trial
  key allows about 10 rerank calls a minute and a monthly cap; Groq about 30 requests a minute;
  Jev is paid. Qdrant: 3 to 4 reads per question.
- **Safe to repeat:** yes. It writes nothing to Qdrant; its SQLite file is deleted.
- **Added in Phase 12: scan what it printed.** Copy the whole output of one run into a text file
  (for example `smoke.txt`; the smoke prints names and counts only) and run:

      uv run python -m app.observability.scan smoke.txt

  You should see `nothing found` and exit code 0. Any `line N: rule` means something that looks
  like an e-mail address, a number, a token or a key was printed: send back the rule names and
  line numbers only (the command never prints the text itself), and do not paste the file.
- **To test Jina alone:** run the customer command once with `COHERE_API_KEY` removed
  (`Remove-Item Env:COHERE_API_KEY`) and look at the `rerank` line's model.

### Run 12b. The role-isolation test (a staff-only document is hidden from a customer)

- **Verifies:** Phases 5 and 8 on real data: a document with `--access-role staff` is never
  retrieved, cited or answered from for a customer, and is for staff; and when it is deleted both
  roles refuse again. The three main questions above cannot show this (all three samples are
  customer documents).
- **Before it:** Run 10 up to (g) for the insurance and banking documents, with step (i) (the
  staff-only copy `ecas-staff-only` is ingested, 21 active chunks), and the insurance and banking
  questions of Run 12 done. The real eCAS customer copy `finserv-mf-ecas` must NOT be in the
  collection yet (Step 0); its question of Run 12 is asked after Step 6.
- **Needs:** the variables of Run 10 (for `ingest`, `status` and `delete`) and of Run 12 (for
  the smoke runs), set in the same window.
- **Why the customer copy goes first:** the staff-only copy holds the same text as
  `finserv-mf-ecas`, which a customer may see. While both exist a customer would be answered
  from the customer copy and the test would prove nothing. So a throw-away customer copy is
  deleted for the test and the real one is ingested at the end. (The ids are only labels: the program separates the two
  copies by id and role.)
- **The question** is one only the eCAS text can answer. It is not in the insurance or banking
  samples: *Which folio is pending nomination opt-in or opt-out?*

**Step 0. The order, and the throw-away customer copy.** A delete records an id as withdrawn
for good, so this drill never deletes a real id. It needs NO other customer copy of the eCAS
text in the collection, so do it **before** the eCAS part of Run 10 (g) (the insurance and
banking documents may be there). Ingest the eCAS as a customer document under
`rehearsal-ecas-1` (the command of Run 10 (g) with `--doc-id rehearsal-ecas-1`), and
`ecas-staff-only` as in (i). The next drill uses `rehearsal-ecas-2`.

**Step 1. Take the customer copy away.**

    uv run python -m app.ingest delete --doc-id rehearsal-ecas-1

It asks you to type the id.

- **You should see:** `rehearsal-ecas-1: removed 21 chunk(s), N allow-list entr(ies); 0 chat
  message(s) flagged 'source removed'`, then `recorded as withdrawn (reason: other); 1 original
  PDF(s) moved`. Then `uv run python -m app.ingest status --doc-id rehearsal-ecas-1` shows
  `stored 0` and `allow-list entries: none`; `status --doc-id ecas-staff-only` still shows
  `active 21`.

**Step 2. Ask as a customer: it must refuse.**

    uv run python scripts/pipeline_smoke.py --run --role customer --question "Which folio is pending nomination opt-in or opt-out?"

- **You should see:** the stage lines up to and including `retrieve OK` and `rerank OK`, then
  `context_guard OK`, then straight to `persist OK` and `respond OK` (no `confidence`,
  `generate` or `output_guard` line: nothing was generated), then

      RESULT               REFUSAL   reason=no_context citations=0 warnings=0 degraded=None cached=False confidence=None tokens=NNN seconds=NN.N
      cited doc ids: none
      review rows: 1
      done: the pipeline did not answer

  Exit code **1** (a refusal is "did not answer": that is the expected result here). The check
  is the two new lines: **`cited doc ids: none`** and **`review rows: 1`**.
- **About `review rows`:** a no-reliable-context refusal is queued for review, and the
  `persist OK` line is where that row is written. The script writes it to its throw-away
  database and prints how many rows it holds (a count: never the row, which holds the masked
  question) just before the file is deleted. Your real database is not touched.
- **If it differs:**
  - `cited doc ids:` shows `ecas-staff-only` (or any id) and `RESULT ANSWER`: a customer was
    answered from a document. Stop, keep the output, and tell your advisor: with the customer
    copy deleted, the only document that can answer is the staff-only one, so this is a real
    isolation failure. Do not go on.
  - `cited doc ids:` shows another id (for example `banking-rtl-facility`): the customer was
    answered from a customer document that happens to mention the same folio (unlikely);
    send the line.
  - `review rows: 0` with `reason=no_context`: the refusal was not queued: send the output.
    `review rows: unknown`: the count could not be read: send the output.
  - `reason=` something else (`out_of_domain`, `unclear`): the question was judged before
    retrieval; send the line.
  - `RESULT UNAVAILABLE`: a provider or store failed; run again.

**Step 3. Ask the same as staff: it must answer, with a citation.**

    uv run python scripts/pipeline_smoke.py --run --role staff --question "Which folio is pending nomination opt-in or opt-out?"

- **You should see:** every stage line as in the main Run 12 list (`triage` through `respond`),
  then

      RESULT               ANSWER    reason=None citations=1 warnings=0 degraded=None cached=False confidence=None tokens=NNN seconds=NN.N
      cited doc ids: ecas-staff-only
      review rows: 0
      done: the pipeline answered

  `citations` is 1 or more. Exit code 0. (The answer text is not printed.) The check is
  **`cited doc ids: ecas-staff-only`**: the staff-only copy, and only that id. `review rows: 0`
  (an answer is not queued).
- **If it differs:** `REFUSAL reason=no_context` as staff means the staff copy is not being
  found: run `status --doc-id ecas-staff-only` (it must say `active 21`) and send both outputs.
  `cited doc ids:` showing another id means the staff answer came from a different document:
  send the line. `warnings=` above 0 or `confidence=low`: the answer was labelled (and
  `review rows: 1` would be the low-confidence row): send the numbers.

**Step 4. Delete the staff-only copy with the delete command.**

    uv run python -m app.ingest delete --doc-id ecas-staff-only

It asks you to type the id.

- **You should see:** `ecas-staff-only: removed 21 chunk(s), N allow-list entr(ies); 0 chat
  message(s) flagged 'source removed'`. Then
  `uv run python -m app.ingest status --doc-id ecas-staff-only` shows
  `chunks: active 0, held back 0, rejected 0, inactive 0 (stored 0)`,
  `allow-list entries: none`, and the marker `no`.

**Step 5. Ask both roles again: both must refuse.**

    uv run python scripts/pipeline_smoke.py --run --role customer --question "Which folio is pending nomination opt-in or opt-out?"
    uv run python scripts/pipeline_smoke.py --run --role staff --question "Which folio is pending nomination opt-in or opt-out?"

- **You should see (both):** the same shape as Step 2: `context_guard OK`, `persist OK`,
  `RESULT REFUSAL reason=no_context citations=0 ...`, **`cited doc ids: none`**,
  **`review rows: 1`**, `done: the pipeline did not answer`, exit code 1. (Each run has its own
  throw-away database, so each shows `review rows: 1` for its own refusal.)
- **If it differs:** an `ANSWER` for staff after the delete, or `cited doc ids:` showing
  `ecas-staff-only`, means a deleted chunk is still retrieved: stop and send the output (this
  would be a deletion or filter fault).

**Step 6. The real customer copy.** Now ingest the real one (the eCAS command of Run 10 (g)).
A routine drill never needs the reinstate flag, and `rehearsal-ecas-1` stays withdrawn.

    uv run python -m app.ingest ingest spec/samples/company/finserv_mutual_fund_ecas_and_servicing.pdf `
      --doc-id finserv-mf-ecas --doc-type policy --access-role customer --product-line finserv

- **You should see:** `finserv-mf-ecas: new 21, changed 0, unchanged 0, removed 0, version 1`,
  exit code 0 (quick: the answers are cached). Then `status --doc-id finserv-mf-ecas` shows
  `active 21`; `status --doc-id ecas-staff-only` still shows `active 21`. The main eCAS question
  of Run 12 is asked now.

- **Send back:** for each smoke command its role, the `RESULT` line, the `cited doc ids:` line,
  the `review rows:` line and the exit code; for each ingest, delete and status the counts
  lines. Never the question's answer text. (The ids are document ids you chose, such as
  `ecas-staff-only`: safe to send.)
- **Clear:** the variables, as in Run 10 and Run 12.
- **Real calls:** four smoke commands in all (Step 2, Step 3 and the two of Step 5). Each
  refusal makes a Jev decision, an embedding and a rerank (about 3 calls); the staff answer
  makes about 4: roughly 13 calls. Steps 1, 4 and 6 are ingest and delete commands: the
  re-ingest in Step 6 is nearly free (cached). Free tiers: as in Runs 10 and 12.
- **Safe to repeat:** yes, from Step 1. If you stop in the middle, `status` on the two ids tells
  you where you are; always finish with Step 6 so that the customer copy is back before
  Run 13.

## Run 13. Pipeline smoke with Langfuse

- **Verifies:** Phase 8 and Change round 1 step 5 (the Langfuse sink sends the trace of a request
  through the Langfuse SDK, with the input, output and metadata of every stage; only masked or
  public text is in it; and a failure never touches an answer).
- **Before it:** Run 12 (the pipeline answers) and a Langfuse project (below). Install the
  new package first (the SDK is pinned in `requirements.txt`): `uv pip install -r requirements.txt`.
- **Create the Langfuse project and keys (once):**
  1. Sign in at your Langfuse cloud account (the free "Hobby" plan is enough) in a browser.
  2. Create an organisation if asked, then a project, for example `bfsi-rag`.
  3. In the project: Settings, API Keys, "Create new API keys". Copy the public key
     (it starts with `pk-`) and the secret key (it starts with `sk-`) straight into your password manager: the
     secret key is shown once.
  4. Note the host of your region, shown on the same page (for example the EU, US or HIPAA
     cloud address).
  5. Make a fourth secret of your own for the user hash. It must be random, at least 32
     characters, and different from `JWT_SECRET_KEY`, `AUDIT_HASH_KEY` and both Langfuse keys:

         $env:LANGFUSE_USER_HASH_KEY = (uv run python -c "import secrets; print(secrets.token_urlsafe(32))")

     Keep it in your password manager too.
- **Needs (names only):** `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`,
  `LANGFUSE_USER_HASH_KEY`, and `JWT_SECRET_KEY` and `AUDIT_HASH_KEY` (read only so that the
  script can check that the user hash key is a different key; set them as in Run 4 or Run 6),
  plus everything Run 12 needs.
- **Commands:**

      $env:LANGFUSE_PUBLIC_KEY = Read-Secret "LANGFUSE_PUBLIC_KEY"
      $env:LANGFUSE_SECRET_KEY = Read-Secret "LANGFUSE_SECRET_KEY"
      $env:LANGFUSE_HOST = Read-Secret "LANGFUSE_HOST"
      $env:LANGFUSE_USER_HASH_KEY = Read-Secret "LANGFUSE_USER_HASH_KEY"
      $env:JWT_SECRET_KEY = Read-Secret "JWT_SECRET_KEY"
      $env:AUDIT_HASH_KEY = Read-Secret "AUDIT_HASH_KEY"
      uv run python scripts/pipeline_smoke.py --run --langfuse --question "What deductible applies to storm and flood claims?"

- **You should see:** the same stage lines as Run 12, the RESULT line, then
  `langfuse             SENT` and `done: the pipeline answered`. Exit code 0.
- **If a line differs:**
  - `langfuse OFF note=all four variables are needed`: one of the four Langfuse variables is
    empty in this window.
  - `langfuse FAILED error=SinkConfigError`: a variable is a placeholder, too short, the host is
    not https, or the user hash key equals or is built from another key. Fix it and run again.
  - `langfuse FAILED error=ExportFailed` (or a class name such as `ConnectionError`): the SDK
    could not deliver the trace: the host, the network or a key is wrong. Check the host (your
    region's address) and the two keys; send this line if it stays.
  - `langfuse FAILED error=Timeout`: nothing was handed over within 20 s.
  - `ModuleNotFoundError: langfuse`: run `uv pip install -r requirements.txt` first.
- **Check in the Langfuse web page** (Project, Tracing):
  1. One new trace named `query` appears, with one observation per stage (`authenticate`,
     `local_scan`, `triage`, `embed`, `retrieve`, `rerank`, `generate` ...), each drawn with its
     own start and end time (the waterfall is the real one). The model calls show a model name and
     token counts; `generate` also shows the cost; `prompt_synthesis` sits inside `generate`.
  2. Open the trace. **Input** is the question and **Output** the answer, both the MASKED stored
     copies (a name you typed shows as `[PERSON]`). Open the three stages that matter:
     - `retrieve`: Input = the question and the search settings (filters, `rrf_k`, the roles
       allowed to see); Output = the chunks found, with ids, documents, clauses and scores.
     - `rerank`: Output = each passage with its text, its rerank score and whether the answer
       used it (`kept_for_the_answer`).
     - `generate`: Input = the instructions, the documents and the question as ONE user message;
       Output = the answer; with the token counts and the model. `prompt_synthesis` inside it shows
       which documents went into the prompt and says the history is `none`.
  3. **There must be NO raw question, no name or number you typed, no rewritten question, no chat
     history or summary, no username or e-mail anywhere**: look at Input, Output, Metadata, Tags,
     and the trace's User and Session fields (long hex hashes, not names). Ask a second question
     in the same chat that needs the first one's history: its trace must not contain the first
     question's words.
  4. To see the old, text-free behaviour, set `LANGFUSE_SEND_TEXT=false` in the window and run
     again: Input and Output are then empty on every observation, with the metadata still there.
- **Send back:** `SENT` (or the failure's error name), and for the Langfuse page only: the
  number of observations, whether Input and Output are filled on `retrieve`, `rerank` and
  `generate`, and "no raw question, name or history found" yes or no. Never a screenshot that shows
  the keys page.
- **Clear:** the variables in this window. The Langfuse keys stay in your password manager;
  revoke them on the API Keys page if they ever leak.
- **Real calls:** the same ~4 model calls as one question in Run 12, plus one delivery to Langfuse
  (about 20 spans). Free tier: the Hobby plan has a monthly cap on units that a trace of this
  size does not approach.
- **Safe to repeat:** yes; each run adds one trace.

## Run 14. Real-Upstash check of the cache code

- **Verifies:** Phase 9 (the cache code behaves on a real Upstash as it does on the test double:
  the version seed and bump, entries with a time to live, reverse sets, batched invalidation, a
  key of the wrong type being a refusal and not an outage, a rate limit, and a refused connection
  being an outage).
- **Before it:** nothing but a working Upstash database. It does not need ingested documents. It
  can be run before or after Runs 9 to 13.
- **Needs (names only):** `UPSTASH_REDIS_URL` (the `rediss://` address).
- **What it touches:** only keys that start with its own throw-away prefix
  `check:<12 hex>:`, made up for each run. It never reads, writes or deletes a real key
  (`ans:`, `chunk:`, `doc:`, `ctr:`, `rl:`, `corpus:version`), never flushes, and removes its own
  keys in a `finally`, even when it fails. The prefix is checked before every use.
- **Commands:**

      $env:UPSTASH_REDIS_URL = Read-Secret "UPSTASH_REDIS_URL"
      uv run python scripts/cache_check.py --run

- **You should see**, one per line (names, PASS or FAIL, and numbers only):

      using a throw-away key prefix check:<12 hex>:
      corpus version seeded from the clock (ms)    PASS
      a bump adds exactly 1                        PASS
      an entry is stored and read back             PASS
      the entry has a time to live                 PASS  ttl_seconds=NNNNN
      it is listed in every reverse set            PASS  sets=3
      invalidating a chunk removes its entry       PASS  removed=1
      a large reverse set is removed in batches    PASS  removed=600 left=0
      a wrong-type reverse set heals, the entry is listed    PASS
      a refusal does not open the 30 s skip window PASS  events=none
      a wrong-type counter heals and counts        PASS  count=1
      the rate limit counts 1, 2 and blocks the 3rd PASS retry_after=NN
      a refused connection opens the 30 s skip window PASS
      30 s skip window length                      INFO  seconds=30.0
      throw-away keys removed                      DONE  removed=NN left=0
      all checks passed

  Exit code 0. (`events=` may list `cache_error` for the refusal checks: that is expected. The
  line that matters is that `cache_unavailable` is NOT among them.)
- **If a line differs:**
  - Any `FAIL`: send the line. The cache code and the real server disagree on something.
  - `throw-away keys removed FAILED` or `left=` above 0: remove the keys named
    `check:<hex>:*` by hand in the Upstash console (Data Browser, search `check:`); they are
    all under that one prefix and nothing else is.
  - `error: UPSTASH_REDIS_URL is not set`: set it in this window.
  - `error: the check stopped (<exception name>)`: the address or the network: send the name.
- **Send back:** the lines above (they hold no key, address or value).
- **Clear:** `Remove-Item Env:UPSTASH_REDIS_URL`.
- **Real calls:** about 2,000 Redis commands (600 entries are made and removed to prove the
  batches). Upstash's free tier allows 10,000 commands a day: do not run it more than twice a day.
- **Safe to repeat:** yes. Each run uses a new prefix and cleans up after itself.

## Run 15. Refresh sparse vectors (only after a tokenizer change)

- **Verifies:** Phase 9 (a changed tokenizer is noticed, and only the sparse vectors are rewritten,
  with no embedding call).
- **When:** only if `SPARSE_VERSION` in `app/vector/sparse.py` was raised (a Claude session will
  tell you), or `ingest status` / the smoke script shows `sparse vectors out of date` above 0.
- **Before it:** Runs 2 and 10 (a collection with chunks). The ingest packages need not be
  installed for this command beyond the base ones.
- **Needs (names only):** `DATABASE_URL`, `QDRANT_URL`, `QDRANT_API_KEY`, `UPSTASH_REDIS_URL`.
  No model key.
- **Commands:**

      uv run python -m app.ingest status --doc-id insurance-flexi-property
      uv run python -m app.ingest refresh-sparse
      uv run python -m app.ingest status --doc-id insurance-flexi-property
      uv run python -m app.ingest refresh-sparse

  (Add `--doc-id NAME` to `refresh-sparse` for one document; add `--load-env` to use your `.env`.)
- **You should see:**
  - first `status`: `sparse vectors out of date: N of M` with N above 0;
  - `refresh-sparse`: `sparse vectors refreshed: N chunk(s) in D document(s) (checked M); no
    embedding call was made`. Exit code 0;
  - second `status`: `sparse vectors out of date: 0 of M`;
  - the last `refresh-sparse`: `sparse vectors refreshed: 0 chunk(s) in 0 document(s) ...` and
    `nothing was out of date: no write, no version bump, no audit row`.
- **If it differs:** `error: cache unavailable, nothing was changed; ...` (exit 1): Upstash did
  not accept the check before the first write: nothing was written; fix Upstash and run it again.
  `refresh incomplete: run the same command again` (exit 1): the corpus
  version was not bumped (Upstash): run the same command again; it finishes the bump.
  `N document(s) have an unfinished ingest, delete or review waiting`: run that command again
  to finish it (the refresh left its marker alone).
  `N chunk(s) have no stored text and were left alone`: send the number.
- **Send back:** the counts lines. Never text.
- **Clear:** the four variables.
- **Real calls:** Qdrant reads and vector updates (about one call per 64 chunks) and 1 Redis bump;
  no model call, no embedding call.
- **Safe to repeat:** yes; with nothing out of date it does nothing.

## Run 16. Local end-to-end chat through the API

- **Verifies:** Phase 10 (the chat endpoint over the real pipeline and real services, the rollout
  stage / shadow mode, sessions and resume, feedback and the review queue, the admin routes, and
  migrations 0006 and 0007 on your real database, and the Phase 10 hardening rules).
- **Before it:** Runs 4 and 5 (keys and a first admin), Run 8 (migrations up to 0005), Runs 9 and
  10 (provider keys work; the three sample documents are ingested) and Run 12 (the pipeline
  answers). Two PowerShell windows.
- **Needs (names only), in window A (the API):** `DATABASE_URL`, `UPSTASH_REDIS_URL`,
  `JWT_SECRET_KEY`, `AUDIT_HASH_KEY`, `QDRANT_URL`, `QDRANT_API_KEY`, `JEV_OPENROUTER_API_KEY`,
  `GEMINI_EMBEDDING_KEY_A`, `GEMINI_API_KEYS`, `GROQ_API_KEYS`, `OPENROUTER_API_KEYS`,
  `COHERE_API_KEY`, `JINA_API_KEY`. The Langfuse variables are optional (the sink is off without
  all four). Window B needs no key, only the passwords you type.
- **Step 1. Apply migrations 0006 and 0007 (window A):**

      $env:DATABASE_URL = Read-Secret "DATABASE_URL"
      uv run python -m app.db.migrate --dry-run
      uv run python -m app.db.migrate

  You should see `pending 0006_rollout_feedback_settings.sql`, `pending
  0007_review_thumbs_down_unique.sql` (and, from Phase 11, `pending
  0008_messages_fixed_reply.sql`) and then `applied` for each. If an earlier file is also
  pending, an earlier migration was not applied (Run 8): let it apply them in order. Migration
  0007 first removes a duplicate thumbs-down review item for one answer, if an earlier double
  click made one (it keeps the first), then adds the index that allows only one.
- **Step 2. Start the API (window A):** set the other variables with `Read-Secret` as in Run 12,
  then:

      uv run uvicorn app.api.main:create_app_from_env --factory

  You should see `Uvicorn running on http://127.0.0.1:8000`. It refuses to start with `refusing to
  start: ...` naming the variable if a secret is missing, short or a placeholder. The name model
  loads in the background (about a minute): until then a chat answers `503 starting_up`.
- **Step 3. Sign in and ask (window B):**

      $base = "http://127.0.0.1:8000"
      function Post($path, $body, $token) {
        $h = @{}; if ($token) { $h["Authorization"] = "Bearer $token" }
        Invoke-RestMethod "$base$path" -Method Post -ContentType "application/json" `
          -Headers $h -Body ($body | ConvertTo-Json)
      }
      $pw = Read-Secret "your admin password"
      $admin = (Post "/auth/login" @{ username = "YOUR_ADMIN"; password = $pw }).access_token
      Post "/auth/register" @{ username = "test.customer"; email = "test.customer@example.test"; password = (Read-Secret "a password for the test customer") }
      $customer = (Post "/auth/login" @{ username = "test.customer"; password = (Read-Secret "same password again") }).access_token

  You should see the register call print `id`, `username` and `role` (`customer`).
- **Step 4. The stage is `shadow` by default, so the customer is not shown the answer:**

      Invoke-RestMethod "$base/admin/shadow" -Headers @{ Authorization = "Bearer $admin" }
      $q = "What deductible applies to storm and flood claims?"
      Post "/chat" @{ message = $q } $customer
      Post "/chat" @{ message = $q } $admin

  You should see `stage : shadow`; the customer's chat returns `answer_visible : False`, `kind :
  shadow_notice` and the fixed notice (no answer text, no citations); the admin's chat returns
  `answer_visible : True`, an answer in `text` and at least one entry in `citations`.
- **Step 5. The hidden answer is kept for you:**

      (Invoke-RestMethod "$base/admin/shadow/answers" -Headers @{ Authorization = "Bearer $admin" }).answers

  You should see one item with the customer's masked `question`, the `answer`, `citations`, and a
  64-character `user` hash (not a name).
- **Step 6. Open the stage and ask again:**

      Invoke-RestMethod "$base/admin/shadow" -Method Put -ContentType "application/json" `
        -Headers @{ Authorization = "Bearer $admin" } -Body '{"stage":"open"}'
      $r = Post "/chat" @{ message = $q } $customer
      $r.answer_visible; $r.citations.Count

  You should see `previous_stage : shadow`, then `True` and a citation count of 1 or more. (The
  stage is cached for up to 5 seconds in the server: wait a few seconds if it still shows the
  notice.)
- **Step 7. Resume, sessions, feedback:**

      $h = @{ Authorization = "Bearer $customer" }
      $sessions = Invoke-RestMethod "$base/sessions" -Headers $h
      $sessions.total
      $id = $sessions.sessions[0].id
      $t = Invoke-RestMethod "$base/sessions/$id" -Headers $h
      $t.messages | ForEach-Object { "$($_.role): $($_.notice)" }
      Post "/messages/$($t.messages[-1].id)/feedback" @{ rating = "down" } $customer
      Post "/messages/$($t.messages[-1].id)/feedback" @{ rating = "down" } $customer

  You should see the session count, the messages with their roles (the first answer, made in
  shadow, shows `notice: shadowed` and the fixed notice text instead of its answer; the later one
  shows no notice), and `rating : down` twice (the second thumb changes nothing: still one).
- **Step 8. The thumbs-down is in the review queue:**

      (Invoke-RestMethod "$base/admin/review?status=open" -Headers @{ Authorization = "Bearer $admin" }).items

  You should see exactly ONE `thumbs_down` item with the masked question (you sent the thumb
  twice). Label it:

      Post "/admin/review/ITEM_ID/label" @{ label = "wrong_answer"; status = "labelled" } $admin

  You should see `label : wrong_answer`, `status : labelled`.
- **Step 9. Admin views (no text, counts only):**

      Invoke-RestMethod "$base/admin/documents" -Headers @{ Authorization = "Bearer $admin" }
      Invoke-RestMethod "$base/admin/alerts" -Headers @{ Authorization = "Bearer $admin" }
      Invoke-RestMethod "$base/admin/users" -Headers @{ Authorization = "Bearer $admin" }

  You should see your three documents with `chunks_active` equal to their stored counts (19, 22
  and 21) and `sparse_vectors_out_of_date : 0`; `banner : False` (or the names of the critical
  alerts, if any); and the accounts without hashes or email addresses.
- **Step 9b. Entity review is off unless you switch it on (optional):** with
  `ENTITY_REVIEW_ENABLED` not set, `PUT /admin/entities/{id}` answers `403 entity_review_disabled`
  and `GET /admin/entities` works. To try it, stop the API, run `$env:ENTITY_REVIEW_ENABLED =
  "true"` in window A, start it again, and approve one entry: a one-word person name answers `409
  single_word_needs_confirmation` until you send `"confirm_single_word": true`. Remove the
  variable again afterwards. (On Render it is always refused.)
- **Step 10. Delete my chats and clean up:**

      Invoke-RestMethod "$base/sessions" -Method Delete -Headers $h
      Invoke-RestMethod "$base/admin/users" -Headers @{ Authorization = "Bearer $admin" }

  You should see `deleted : N` and, afterwards, `GET /sessions` showing `total : 0`. Turn the test
  customer off so it cannot be used: `PUT /admin/users/{id}/active` with `{"active": false}`. Put
  the stage where you want it for now (`shadow` is the safe default). Stop the API with Ctrl+C.
- **If a line differs:**
  - `503 starting_up`: wait and try again; if it persists for several minutes, send the server
    window's last lines.
  - `503 service_unavailable`: a store or provider is down; the server window shows which stage
    failed (names only). Send it.
  - `429 too_many_requests` after 20 chats in a minute is expected.
  - Step 4 customer answer visible although the stage says `shadow`: stop and send the output (a
    rollout fault).
  - Any 500: send the exception name from the server window (no text is logged).
- **Send back:** for each step the status words and counts only (stage, `answer_visible`, citation
  count, totals, labels). Never the answer text, a token or a password.
- **Clear:** close both windows. Remove the variables as in the clearing list at the top.
- **Real calls:** about 4 model calls per chat (a Jev decision, an embedding, a rerank and an
  answer; a repeated question can be served from the cache with fewer). Six chats in all: roughly
  25 calls. Free tiers: as in Run 12.
- **Safe to repeat:** yes (use a new test username, or reuse the account and skip registering).


## Run 17. Look at every screen of the Streamlit frontend

- **Verifies:** Phase 11 (the screens, in a real browser: the look in the light and dark themes,
  the layout on a narrow window, keyboard focus, the waiting states, and that the admin
  controls appear only where they should). The tests cannot draw a page, so this is the check of
  what you actually see.
- **Before it:** Run 16 (the API runs locally with a database that has the migrations up to
  0008, an admin account and the three sample documents; apply 0008 with `uv run python -m
  app.db.migrate` as in Run 16 step 1), and Run 12's variables for the API. Two PowerShell
  windows: A for the API, B for the frontend.
- **Needs (names only):** window A: as in Run 16. Window B: `API_BASE_URL` (optional; the
  default is the API on this machine) and `ADMIN_MACHINE`. No key and no password is set in
  window B: you type passwords into the page.
- **Step 1. Install the frontend packages (window B):**

      uv pip install -r requirements-frontend.txt

  This installs only `streamlit`, `httpx` and what they need. It should not install torch, spaCy
  or anything of the API (`uv pip list` shows them from earlier phases; they are not this
  service's).
- **Step 2. Start the API (window A):** as in Run 16, step 2. Wait for `Uvicorn running`.
- **Step 3. Start the frontend as a customer would see it (window B):**

      Remove-Item Env:ADMIN_MACHINE -ErrorAction SilentlyContinue
      uv run streamlit run frontend/app.py

  A browser tab opens at `http://localhost:8501`. You should see the sign-in page: a "BFSI
  Assistant" title, two tabs ("Sign in", "Create account"), and no warning about usage statistics.
- **Step 4. Create a customer account and sign in.** Create an account in the second tab, then sign
  in. You should see the chat page with "Ask about a policy or regulation" at the bottom and your
  name and "(Customer)" in the left column. No admin sections are shown.
- **Step 5. Ask.** Ask `What deductible applies to storm and flood claims?`. While it works you
  should see "Checking your question", then other stage names and a timer in seconds. Then the
  answer card: the answer, source chips such as `doc-id · clause 4.2 · page 7`, and, where the
  service says so, notices in words (a warning, a reduced-mode notice, the disclaimer as a small
  footnote). Click "👍 Helpful": you should see "Thank you, your feedback was noted." and the
  buttons go away. (While the rollout stage is `shadow` you will see the "Test period" notice
  instead of an answer: that is correct; open the stage as an admin in Step 8 and ask again.)
- **Step 6. Chats.** Click "New chat", ask another question, then click the first chat in the list:
  it should open with its earlier messages. Click "Delete this chat": you should be asked
  "Delete this chat?" and nothing is deleted until you click "Yes, delete". Click "Keep" to cancel.
- **Step 7. Themes, narrow window, keyboard.** In the browser's menu, switch the theme to dark and
  back: all text should stay easy to read in both. Make the window narrow (about 360 px): nothing
  should scroll sideways and the text keeps a small margin on both sides. Press Tab repeatedly: a
  thick blue outline should show on whatever has focus, in both themes.
- **Step 7b. Contrast of Streamlit's own text.** The tests check the colours the page itself
  sets. Streamlit draws some text itself (the small grey notes such as "No chats yet.", the
  page numbers under a table, "Feedback is not available for this answer yet."). Use a contrast
  tool in the browser (for example the contrast checker in the browser's developer tools, or a
  colour picker extension) on two or three of these notes, in the light and the dark theme. You
  should see a ratio of at least 4.5. Send back the ratio and the theme for any that is lower.
- **Step 7c. The settings file.** In window B, stop the frontend and start it from a different
  folder: `cd frontend` then `uv run streamlit run app.py`. The page should show "Safety settings
  may not be active". Go back to the project folder and start it again: the notice is gone.
- **Step 8. Admin view.** Sign out. Sign in as your admin account. You should see a row of NINE
  sections: Chat, Documents, Entities, Review queue, Users, Alerts, Dashboards, Rollout,
  Evaluation. Open each: Documents shows your three documents with counts and says "done on the
  admin machine"; Entities is read-only; Review queue lists items and has a "Label" box with five
  fixed labels; Users shows accounts (no hashes or emails); Alerts shows "All quiet" or the
  critical events; Dashboards shows six panels (Decider in use, Key pools, Tokens and cost, Cache,
  Ingestion health, Daily metrics), each with a table above its chart (Run 18 looks at them
  closely); Rollout shows the stage and lets you change it; Evaluation says "Not available yet".
- **Step 9. The admin machine view.** Stop the frontend (Ctrl+C in window B) and start it as the
  admin machine:

      $env:ADMIN_MACHINE = "true"
      uv run streamlit run frontend/app.py

  Sign in as admin: Documents now also shows the exact ingest and delete commands to copy (the
  page never runs them), and Entities shows "Approve" and "Reject". Approving needs
  `ENTITY_REVIEW_ENABLED=true` in window A (Run 16, step 9b); without it the page says "Entity
  review is not switched on on this machine." Remove the variable again afterwards.
- **Step 10. The service waking up.** Stop the API (Ctrl+C in window A), then ask a question in the
  page. You should see "The service is waking up" with a timer (not an error), and after up to
  about 75 seconds "The service is still not ready. Please try again in a minute." Start the API
  again and ask once more: it answers.
- **If a line differs:**
  - A blank page or a red error box: send the last lines of window B (the page itself shows only
    a generic message by design).
  - "Set-up problem: API_BASE_URL must be a web address": fix the variable and restart.
  - Text hard to read in one theme, a sideways scroll at 360 px, or no focus outline: send a
    screenshot and the theme name.
  - Any answer that shows a link you can click, or markup characters turned into formatting: stop
    and send a screenshot (a rendering fault).
- **Send back:** for each step "ok" or what differed, and screenshots only of layout problems.
  Never a token, a password or an answer's text.
- **Clear:** close both windows and the browser tab. Remove `ADMIN_MACHINE` and
  `ENTITY_REVIEW_ENABLED` from your shell. Nothing is written to disk by the frontend.
- **Real calls:** the same as Run 16 for each question you ask (about 4 model calls each). Four to
  six questions in all.
- **Safe to repeat:** yes.


## Run 18. Observability: the webhook, the log scan, trace deletion, the purge, the dashboards

- **Verifies:** Phase 12 on your real services: migration 0009, the chat webhook (one critical test
  alert arrives, in the shape you chose), the PII and secret scan on a real log file, the removal
  of a deleted chat's traces from real Langfuse (this checks the two Langfuse routes the code
  uses), the owner-run purge, the dev stage printer and the Dashboards section.
- **Before it:** Run 16 (the API runs locally with the migrations), Run 17 (the frontend), Run 13
  (Langfuse receives traces: the four `LANGFUSE_` variables set) and a chat tool where you can make
  an incoming webhook (a throw-away test channel is best). Two PowerShell windows: A (API), B
  (frontend and commands).
- **Needs (names only), in addition to Run 16:** `ALERT_WEBHOOK_URL` (a secret: set it with
  `Read-Secret`, never type it where it is shown), optionally `ALERT_WEBHOOK_FORMAT` (`text` or
  `discord`), and for step 6 `LANGFUSE_USER_HASH_KEY` and the other three Langfuse names in
  window B too. `DEV_STAGE_TRACE` for step 8.
- **Step 1. Apply migration 0009 (window A):**

      uv run python -m app.db.migrate --dry-run
      uv run python -m app.db.migrate

  You should see `pending 0009_observability.sql`, then `applied 0009_observability.sql`.
- **Step 2. Start the API with the webhook (window A):** set the variables, then
  `uv run uvicorn app.api.main:create_app_from_env --factory`. You should see JSON log lines (one
  object per line). If you see `refusing to start: ALERT_WEBHOOK_URL must be an https address` (or
  `is still a placeholder`), fix the address; the message never shows it.
- **Step 3. A test message reaches the chat.** In window B, with `ALERT_WEBHOOK_URL` (and
  `ALERT_WEBHOOK_FORMAT`) set as in window A:

      uv run python -m app.observability.webhook --test

  You should see `sent` and one message in the chat tool, `BFSI assistant [test] webhook check`.
  If you see a class name instead (for example `HTTPStatusError` or `ConnectError`) the address or
  the format is wrong for that tool. The command never prints the address. This sends nothing to
  the banner and writes nothing to the database. (A real alert is sent when a critical event or the
  error-rate rule fires; the tests cover the rules, and the banner shows them in Alerts.)
- **Step 4. Look at the log with the scan.** Redirect window A's output to a file when you start the
  API (`... 2> api.log`), ask two or three questions in the frontend, stop it, then:

      uv run python -m app.observability.scan api.log

  You should see `nothing found` and exit code 0. If it lists `line N: rule`, send back those
  lines (rule names and numbers only).
- **Step 5. The dashboards.** Start the frontend as in Run 17 and sign in as admin. Open
  Dashboards: each panel has a table above its chart. Decider in use shows the providers and
  their state; Key pools shows counts only; Tokens and cost shows billed cost and shadow cost in
  separate columns; Daily metrics shows today's row after about five minutes (the service saves
  numbers every five minutes).
- **Step 6. Trace deletion on the real Langfuse (the check of its routes).** In window B, with
  the four Langfuse names set to the same values as the API:
  1. Ask one question as the test customer so that a trace is made (wait a minute; check in the
     Langfuse project that one trace appeared with a `sessionId` of 64 hex characters).
  2. Delete that chat in the frontend (My chats, Delete this chat).
  3. Wait up to two minutes (the worker runs every minute).
  You should see the trace gone in Langfuse. (The service keeps its own note of the chat for at
  least 10 minutes and looks once more before dropping it, so a late trace is caught; that note
  holds only a hash and is not visible in Langfuse.) If it is still there after five minutes: open the
  API window, look for `trace_deletion_failed` after five failed tries (an alert), and send back
  the status words only (for example `401`, `404`, `405`). A `404` or `405` from the list or
  delete route means the documented route differs in your Langfuse version: send it back and the
  routes in `app/observability/traces.py` are adjusted.
- **Step 7. The purge (owner-run).** In window B:

      uv run python -m app.memory.retention --dry-run
      uv run python -m app.memory.retention

  You should see two lines, `would delete N chats (M messages)` and `would delete N audit rows, N
  alert rows and N hourly metric rows older than 30 days`; then the same with `deleted`. A
  `traces:` line appears when chats were queued.
- **Step 8. The stage printer.** In window A (not on Render) set `DEV_STAGE_TRACE` to `true`, start
  the API, ask one question. You should see lines `> triage`, `< triage ... ms ...` for every
  stage, then a `waterfall:` with the slowest stage and the unaccounted time, and never the text of
  your question. Unset it again.
- **If a line differs:**
  - No message in the chat tool but the banner shows the alert: the webhook address or format is
    wrong for that tool; the API window shows `alert_webhook_failed` with a class name (for
    example `HTTPStatusError`). Send the class name.
  - A line of the log scan: send the rule name and line number only.
  - A dashboard panel empty after questions: the numbers are per process and restart empty: ask a
    question again after a restart.
- **Send back:** for each step "ok" or what differed (status words, class names, rule names and
  line numbers). Never the webhook address, a token or any text from the logs.
- **Clear:** unset `ALERT_WEBHOOK_URL`, `ALERT_WEBHOOK_FORMAT`, `DEV_STAGE_TRACE` and the Langfuse
  names; delete `api.log` (it holds nothing sensitive by design, but it is a log); revoke or delete
  the test webhook in the chat tool; put the key variables you changed in step 3 back.
- **Real calls:** about 4 model calls per question; step 3 makes no model call that succeeds (the
  keys are invalid on purpose); one question for step 6 and one for step 8: about 10 calls in all.
  One webhook message. A few Langfuse list and delete calls.
- **Safe to repeat:** yes. Use a throw-away channel for the webhook.

## Run 19. Evaluation set-up: the test collection and the sample documents: PENDING

- **Verifies:** Phase 13 on your real Qdrant: the 60-case set and the corpus files check out; the
  local evaluation database is created; the separate test collection `documents_eval` exists on
  your cloud Qdrant; the three sample documents are ingested into it (and NOT into `documents`).
- **Before it:** Run 2 (Qdrant works) and Run 10 (an ingest has worked once, so you know the
  ingest command and its keys). No evaluation key is needed for this run.
- **Needs (names only):** `QDRANT_URL`, `QDRANT_API_KEY`, the two embedding keys
  (`GEMINI_EMBEDDING_KEY_A`, and `_B` if you have it), `GEMINI_API_KEYS` (the image captions), and
  a **local** `DATABASE_URL` pointing at a SQLite file, for example
  `sqlite:///eval_data/ingest.db` (so the allow-list and audit rows of a test ingest never reach
  your main database; the command refuses a remote `DATABASE_URL` for a test collection).
  Do NOT set `UPSTASH_REDIS_URL` for this run: a test ingest never touches the cache.
- **Step 1. Check the set (no call):**

      uv run python -m app.eval check

  You should see `dataset 2026-10-v1: 60 cases, 20 active` and a line saying which cases you
  should read yourself, and exit code 0. If it prints `problem: ...` lines, send them back.
- **Step 2. Create the local evaluation database and put the set in it (no call):**

      uv run python -m app.eval migrate
      uv run python -m app.eval cases-sync

  You should see a line naming `0001_evaluation.sql`, then `local evaluation database: 1
  file(s) applied`, and for the second command `local: 60 cases of dataset 2026-10-v1 written`.
  The file is `eval_data\eval.db` (git-ignored). If you ran an earlier version of this command,
  nothing needs doing: the first version of this table set was never applied anywhere.
- **Step 3. Create the test collection (REAL Qdrant, `documents_eval` only):**

      uv run python -m app.vector.documents --ensure --collection documents_eval

  You should see `collection 'documents_eval': created` and the payload indexes. The command
  refuses `documents` and any name that does not match `^[a-z0-9]+_eval$` (lowercase letters and
  digits, then `_eval`: `documents_eval` is right, `Documents-Eval` or `my_docs_eval` is not).
- **Step 4. Make a local database for the test ingest, and ingest the three documents:**

      $env:DATABASE_URL = "sqlite:///eval_data/ingest.db"
      uv run python -m app.db.migrate
      uv run python -m app.ingest ingest spec\samples\company\banking_rtl_standard_facility_agreement.pdf --doc-id banking-rtl-facility --doc-type policy --access-role staff --product-line banking --collection documents_eval
      uv run python -m app.ingest ingest spec\samples\company\finserv_mutual_fund_ecas_and_servicing.pdf --doc-id mf-ecas-servicing --doc-type policy --access-role customer --product-line mutual_funds --collection documents_eval
      uv run python -m app.ingest ingest spec\samples\company\insurance_flexi_commercial_property.pdf --doc-id insurance-flexi-property --doc-type policy --access-role customer --product-line insurance --collection documents_eval

  Use `--dry-run` first on each if you like. These are the document ids, roles and product lines
  of the corpus manifest (`app/eval/data/corpus_manifest.json`); the cases name them. If a held-
  back item asks for review, use `uv run python -m app.ingest review entities --doc-id ... --collection documents_eval`.
- **Step 5. See what is in the test collection:**

      uv run python -m app.ingest status --doc-id mf-ecas-servicing --collection documents_eval

  You should see counts of active chunks (no text). Do the same for the other two ids.
- **If a line differs:** `refusing ... DATABASE_URL to be a local SQLite file` means step 4's
  variable is missing; `must match ^[a-z0-9]+_eval$` means the collection name was mistyped (it
  is the same rule for `INGEST_COLLECTION`, which `--collection` sets for one run); a free-tier
  message from the embedding or caption call is safe to repeat later.
- **Send back:** the output of step 1, and for step 5 the three status outputs (counts only).
- **Clear:** `Remove-Item Env:DATABASE_URL` and the key variables you set. `eval_data\` stays
  (it is your local evaluation data).
- **Real calls:** about 20 to 40 embedding calls and a few caption calls for the three documents.
- **Safe to repeat:** yes (an unchanged document is a no-op).

## Run 20. Evaluation calibration (needs your yes: real evaluation keys): PENDING

- **Verifies:** the judge model works through the gateway on the evaluation key pool; how well the
  judge model and Jev agree with the 30 hand labels; which metrics Jev may decide (85 % [T]).
- **Before it:** Run 19. Review the 10 calibration items marked `review` in
  `app/eval/data/calibration.json` (the file says which): they are the expert-review sample [T];
  correct any label you disagree with, then run `uv run python -m app.eval check`.
- **Needs (names only):** `EVAL_GROQ_API_KEYS`, `EVAL_GEMINI_API_KEYS`,
  `EVAL_OPENROUTER_API_KEYS` (separate from the production keys), `JEV_OPENROUTER_API_KEY` (Jev's
  own key), `GEMINI_EMBEDDING_KEY_A`, and `EVAL_JUDGE_MODEL`: an OpenRouter model of a DIFFERENT
  family than Groq `gpt-oss`, Gemini and Gemma (not any OpenAI, Google or Gemma-family model;
  for example a Llama, Qwen or Mistral model). The gateway refuses to start otherwise.
  Set the keys with `Read-Secret`; `EVAL_JUDGE_MODEL` is a name and can be typed.
- **One call first (optional, 1 request of the key's daily allowance):** to check only that the
  judge key and model work, before the whole run:

      uv run python scripts/judge_smoke.py --run

  It makes ONE tiny call through the judge path of an evaluation run (the `eval_openrouter` key
  pool and `EVAL_JUDGE_MODEL`) and prints the status, the model that answered, the key's short
  fingerprint, the token counts and the time; never the key. `EVAL_OPENROUTER_API_KEYS` and
  `EVAL_JUDGE_MODEL` must be set in this window (or listed in your own settings file, which the
  script reads for those names only, as `gateway_smoke.py` does).
- **Step 1. The estimate (no call):**

      uv run python -m app.eval calibrate

  You should see the requests and tokens for the judge model and for Jev against their daily
  limits (about 30 and 30), and the line `To run it: calibrate --confirm CODE`. The estimate
  names which groups share their daily limits with production (Jev, embeddings, rerank): this
  run's calls count against those limits and the day's check counts them too.
- **Step 2. Run it (about 60 model calls) only if the estimate looks right:**

      uv run python -m app.eval calibrate --confirm CODE

  You should see one line per metric (five): the judge's agreement, Jev's agreement with how many
  of the 6 items Jev answered, Jev against the judge (for information only), and `Jev trusted:
  yes` or `no`. Jev is trusted for a metric only when it answered at least 5 of the 6 items (an
  outage is a miss) AND agrees with your labels at 85 % [T] or more. The result is stored with
  the Jev and judge model names: if you change either model, that calibration no longer counts
  and the metric goes back to the judge model until you run this again. The run is recorded
  (`status` shows it as `calibration`) and its calls count against today's limits.
- **If a line differs:** `stopped by the cost guard` means the run used more than 25 % over its
  estimate and stopped: send the numbers. A metric with `Jev agreement None` means Jev did not
  answer (an outage or the key): that metric stays with the judge model.
- **Send back:** the five lines (metric names and percentages).
- **Clear:** unset the key variables.
- **Real calls:** about 60 (30 to the judge model, 30 to Jev). The judge pool is OpenRouter's free
  tier: 50 requests a day per key, 20 a minute.
- **Safe to repeat:** yes; each repeat uses another 60 calls.

## Run 21. The first baseline and the variance band (needs your yes: real keys): PENDING

- **Verifies:** the whole system on the evaluation set with real models, the cost estimate against
  the real use, and the noise band (the active set run three times, unchanged).
- **Before it:** Run 20, and the same variables. Do this on a day you have not used the free tiers.
  Do not change the set or the system between the three runs.
- **Step 1. The estimate (no call). It also makes the run:**

      uv run python -m app.eval estimate --set active

  You should see a table of requests and tokens per provider against the daily limits, how many
  days it needs (the judge pool's 50 requests a day per key is the tightest), the number of
  batches, and `To start it, type the confirmation code: run RUN_ID --confirm CODE`. Nothing has
  been called. Use `--no-judge` for a rules-only run that is cheap.
- **Step 2. Start (one batch a day):**

      uv run python -m app.eval run RUN_ID --confirm CODE

  You should see `run RUN_ID: paused; finished N case(s); R requests, T tokens used` (more
  batches wait) or `run RUN_ID: finished; ...`. The code is valid for 30 minutes from its
  estimate; after that the command says `not started: the code is older than 30 minutes` and you
  make a new estimate. A run that is already running says `not started: this run is already
  running`; if its window was closed by a crash, the same command works again after 10 minutes.
  Every model call is counted and stored as it is made, so a stop or a crash loses none of the
  day's count. On the next day (UTC) run the same command again for the next batch (a run that
  was confirmed once is not asked for a new code).
- **Step 3. The band:** make three runs with `--set variance` (an estimate for each, each started
  with its own code, all three on the same system and the same set), then open Run 23's page.
- **Step 4. Look at the numbers:**

      uv run python -m app.eval status

- **If a line differs:** `stopped by the cost guard` (used more than 25 % over the estimate): send
  the numbers; the placeholders in the estimate are then corrected. `today's ... limit would be
  passed; try tomorrow (UTC)` is the guard keeping the free tier safe.
- **Send back:** the `status` lines and, for each finished run, the golden score.
- **Clear:** unset the key variables.
- **Real calls:** about 600 to 1,000 per full judged run of the active set (spread over several
  days); a rules-only run is about 110.
- **Safe to repeat:** yes, with a new estimate each time. A baseline changes the targets from
  "baseline" to numbers only when you decide it.

## Run 22. The production sample (needs your yes: real keys): PENDING

- **Verifies:** the separate cloud evaluation database, the 5 % sample written by the running
  service (masked text only), judging it on this machine, the weekly drift report and the
  30-day purge.
- **Before it:** Run 21. Create a second free Postgres database (Neon or Supabase) for evaluation
  only: **not** the main database.
- **Needs (names only):** `EVAL_DATABASE_URL` (its direct URL with `sslmode=require`; a secret:
  `Read-Secret`), `EVAL_SAMPLE_RATE` (for this run set `1` so every answer is kept, then put it
  back to the default 0.05).
- **Step 1. Create its tables:**

      uv run python -m app.eval migrate --production

  You should see a line naming `0001_evaluation.sql` and `production evaluation database: 1
  file(s) applied`.
- **Step 2. Start the API with `EVAL_DATABASE_URL` and `EVAL_SAMPLE_RATE=1` set** as in Run 16,
  ask three questions in the frontend (one with a made-up PAN in it).
- **Step 3. Check the samples are masked:**

      uv run python -m app.eval status --production
      uv run python -m app.eval drift --production

  `drift` should say `production: 3 sample(s) this week, 0 in the baseline window`, a note
  `too few samples to compare: at least 20 in each window` and `alerts: none`, and it exits with
  code 0 even when it names alerts (they are alerts only). It also saves a snapshot, so next
  week's report has a baseline window. The made-up PAN must not appear anywhere: ask your advisor
  to run the log scan on the API log, and look at the Evaluation page's Production view (no text
  is shown there).
- **Step 4. Judge the queue (needs the evaluation keys; shows an estimate first):**

      uv run python -m app.eval judge-samples --limit 20 --production
      uv run python -m app.eval judge-samples --limit 20 --production --confirm CODE

  You should see the estimate, then `judged 3 sample(s)`.
- **Step 5. The purge:**

      uv run python -m app.memory.retention --dry-run

  With `EVAL_DATABASE_URL` set you should see, after the chat and audit lines, `would delete 0
  masked evaluation samples older than 30 days`. The chat and audit sweep runs first; if the
  evaluation database cannot be reached you see `evaluation samples: not purged (ClassName); the
  chat and audit parts above are done` and the command still exits 0.
- **If a line differs:** `eval_sample_failed` in the API window (a class name) means the sample
  could not be written: check the address and that step 1 ran.
- **Send back:** the `status` and `drift` lines and the judged count. Never the address.
- **Clear:** unset `EVAL_DATABASE_URL` and `EVAL_SAMPLE_RATE`, and put `EVAL_SAMPLE_RATE` back on
  Render (or leave it unset: the default is 5 %).
- **Real calls:** three questions (about 12 calls) and the judging (about 60 calls).
- **Safe to repeat:** yes.

## Run 23. The Evaluation page, the "Run eval" button and "add to evaluation set": PENDING

- **Verifies:** the admin Evaluation section in a browser: the two views, the tables beside the
  charts, the estimate and the typed-code start, and the review queue's one-click add.
- **Before it:** Run 17 (the frontend), Run 21 (some finished runs) and Run 22 for the production
  view.
- **Needs:** window A: the API as in Run 16 with the evaluation keys set too (the page's start
  button runs `python -m app.eval run ...` as its own process with this window's settings);
  window B: the frontend with `ADMIN_MACHINE=true`.
- **Steps:**
  1. Sign in as admin, open Evaluation. You should see the Local test view (and Production if
     `EVAL_DATABASE_URL` is set): golden-set score, noise band, runs, per-category scores, the
     stage latency, model mix, the guardrail table, calibration and drift, each with its table.
  2. Under "Run an evaluation" press "Estimate (no call is made)": the table and a code appear.
     Type the code and press "Start the run" within 30 minutes: the notice says it started;
     reload to see progress. A second press says the run is already running.
  3. Review queue: pick an item, set a label, press "Add to evaluation set". You should see a
     notice titled "Added to the evaluation set" (it waits as a candidate), or "Already in the
     evaluation set". If no evaluation database is set up here, or the question is not added, the
     notice is titled "Label saved, not added" and the item stays "labelled", never "added". The
     set is unchanged (candidates join only when a new dataset version is made).
- **If a line differs:** "The evaluation keys are not set on this machine" means window A lacks
  the evaluation variables; "That code does not match" means the code was mistyped.
- **Send back:** what you saw for each of the three steps (a sentence each).
- **Clear:** close both windows.
- **Real calls:** the start button runs a real evaluation (see Run 21); the other steps make none.
- **Safe to repeat:** yes.

## Run 24. Secret scan on this machine before a push (a pre-push check): PENDING

- **Verifies:** that the history of this repository has no secret in it, with the same program,
  the same version and the same settings as the CI job, so a red scan is found here and not
  after the push.
- **Before it:** nothing. It makes no model call. It reports file names, line numbers and rule
  names, never the value (`--redact`).
- **Needs:** gitleaks 8.30.1 (the version CI installs) on this machine, once:
  1. Open https://github.com/gitleaks/gitleaks/releases/tag/v8.30.1 and download
     `gitleaks_8.30.1_windows_x64.zip` and `gitleaks_8.30.1_checksums.txt`.
  2. Check the file in PowerShell and compare the result with the line for that zip in the
     checksums file (they must be identical):

         (Get-FileHash .\gitleaks_8.30.1_windows_x64.zip -Algorithm SHA256).Hash

  3. Unzip it and put `gitleaks.exe` in a folder that is on your PATH (or run it by its full
     path). `winget install Gitleaks.Gitleaks` also works; then `gitleaks version` must say
     8.30.1 (another version can read the settings file differently).
- **Step 1. Scan the whole history, from the repository root:**

      gitleaks git --redact --no-banner --verbose

  This is the CI command. It reads `.gitleaks.toml` (the default rules plus three narrow path
  allowances) and `.gitleaksignore` (fingerprints of earlier findings) from the repository root
  by itself.
- **Step 2 (optional, quick). Scan only the changes you have staged and not committed yet:**

      gitleaks git --redact --no-banner --verbose --staged

- **You should see:** a count such as `37 commits scanned` and `no leaks found`, exit code 0.
- **If a line differs:** a finding prints `Finding:` (redacted), `RuleID:`, `File:`, `Line:`,
  `Commit:` and a `Fingerprint:`. Send me the `Fingerprint:` and `File:`/`Line:` lines (never
  a value; it is redacted anyway). A real secret means: do not push, and tell me at once. A
  false positive is fixed in the code (rename the variable) or, only when the name cannot
  change, in `.gitleaks.toml` or `.gitleaksignore` by me.
- **Send back:** `no leaks found`, or the lines above.
- **Clear:** nothing to clear.
- **Real calls:** none.
- **Safe to repeat:** yes; run it before every push.

## Run 25. Record the provider cassettes (needs your yes: real keys): PENDING

- **Verifies:** that the real client code reads what the five real providers really answer. The
  recorded tests replay these recordings with the network switched off; until you record them
  they are skipped with the message `cassette ... is not recorded yet`.
- **Before it:** nothing else. Do not hand-write a cassette: a hand-written one only tests our
  own guess of the answer.
- **Needs (names only):** `COHERE_API_KEY`, `JINA_API_KEY`, `JEV_OPENROUTER_API_KEY`,
  `GEMINI_EMBEDDING_KEY_A`, `OPENROUTER_API_KEYS` (the first one is used). Set them with
  `Read-Secret` in this window only; never put them in a file.
- **Step 1. The plan (no call):**

      uv run python scripts/record_cassettes.py

  You should see `plan: 5 real call(s), one per provider`, one line per provider with `key set`
  or `key NOT set`, and `nothing was called: add --run to record`. With `--run` and a key
  missing, nothing is called and it prints `not started, these settings are missing: NAMES`.
- **Step 2. Record (five real calls):**

      uv run python scripts/record_cassettes.py --run

  You should see one line per provider such as `cohere_rerank: recorded (1234 bytes), hygiene
  rules passed`, and `5 of 5 cassette(s) recorded`. The files are in `tests\cassettes\`. The
  recording removes every key header and `key=` parameter, cuts each embedding vector to its
  first 8 numbers (the original length is kept beside it and restored on replay), replaces long
  random-looking tokens and drops every response header but the content type. A file that still
  breaks one of these rules is deleted and the line says `DELETED`.
- **Step 3. Scan the folder with gitleaks (Run 24 explains the install) and replay it:**

      gitleaks dir --redact --no-banner --verbose tests\cassettes
      uv run pytest tests\test_cassette_hygiene.py tests\test_recorded_providers.py -q

  You should see `no leaks found`, then all tests passing and no `skipped` for a cassette.
- **If a line differs:** `key NOT set` names the setting to add. `FAILED (ClassName)` is a
  provider error (a wrong key, a limit): send me the class name; nothing was written for it.
  `DELETED` means the hygiene rules caught something: send me the line. A replay test that fails
  after a good recording means our parser and the provider disagree: send me the test name.
- **Send back:** the output of steps 1 to 3. Do not commit the folder: I read the structure of
  each file (no keys appear in it) and then commit it with you.
- **Clear:** unset the five variables.
- **Real calls:** 5 (Cohere rerank 1, Jina rerank 1, Jev decisions 1 (a fraction of a cent),
  Gemini embeddings 1 call of 2 texts, OpenRouter chat 1 on the free Gemma model).
- **Safe to repeat:** yes; each run overwrites the cassettes (and uses 5 calls again).

## Run 26. Stubbed-model load test on this machine: PENDING

- **Verifies:** how the CODE behaves when users arrive in steps (10, 20, 40, 80, 160): threads,
  the event loop, the password-hashing gate, connection use, locks. It does **not** show how big
  a host must be, and it does not exercise a cloud database or a real provider's limits: the
  report states this itself.
- **Before it:** nothing. No key, no cloud address, no quota. The server refuses to start while
  any store address in this window points at a remote host.
- **Which stores are written:** a throw-away SQLite file in a temporary folder (the load users,
  their chats), deleted when window A stops, and `load_out\` in the project folder (the users'
  made-up credentials and the run's CSV files; git-ignored). Nothing else, nothing in the cloud.
- **Needs:** two PowerShell windows in the project folder, and about 10 minutes.
- **Step 1. Window A, start the stubbed API (the first start loads the name model, about a
  minute). It makes the load users first, in its own SQLite file, with the application's account
  function; nobody registers through the API (the registration limit per address stays on):**

      uv run python load/stub_server.py

  You should see `stub server on http://127.0.0.1:8099 (scripted model, 300 ms per answer,
  SQLite, in-memory vector store, 160 accounts made)`. Leave it running.
- **Step 2. Window B, the users (the default plan is five one-minute steps). The flag
  `--csv-full-history` is needed: without it Locust writes the whole run's percentiles, which
  hide a step that got slow late:**

      uv run locust -f load/locustfile.py --headless --host http://127.0.0.1:8099 --csv load_out/run --csv-full-history

  You should see the banner `STUBBED MODEL: ...`, then Locust's table every few seconds, and the
  run ends by itself after the last step. A shorter plan, set in BOTH windows before step 1:
  `$env:LOAD_STEPS = "10x20,20x20,40x20"` (a step is at least 20 seconds).
- **Step 3. Where did it break:**

      uv run python load/report.py load_out/run

  You should see one line per step: users, requests, failed %, unavailable %, active %, limited
  (the per-user rate limit, by design, not a failure), p50, p95 and ok or BROKE, then either
  `it did not break up to N users` or `first step that broke: N users (reason)`, and the list of
  what this run cannot show. A step breaks when more than 1 % failed, more than 1 % of answers
  were "temporarily unavailable", p95 passed 30 s, or fewer than 80 % of its users were active.
- **If a line differs:** `refused: ... listens on this machine only` means the host is not
  127.0.0.1. `refused: these settings point at a remote host: NAME` means unset that variable in
  window A. `WARNING: ... CUMULATIVE percentiles` means step 2 was run without
  `--csv-full-history`. A step that broke: send me the report and the last 30 lines of window A
  (they hold no text of a question).
- **Send back:** the report (it holds numbers and names only).
- **Clear:** press Ctrl+C in window A (the SQLite file goes with it); delete `load_out\`.
- **Real calls:** none.
- **Safe to repeat:** yes.

## Run 27. Real-provider load test, short, on this machine (needs your yes): PENDING

- **Verifies:** the same users against the real providers, to see rate limits, retries and
  latency that a scripted model cannot show. It is short on purpose: every step spends free-tier
  quota (the estimate shows by how much).
- **Before it:** Run 26 and Run 16 (the providers work from this machine). It is **not** run
  against your normal local API of Run 16 and never against the deployed address: capacity of the
  deployed service is measured in Phases 15 and 16 with a short real run you approve then.
- **Which stores are written, exactly:**
  - accounts, chats, messages, audit rows: a throw-away SQLite file in a temporary folder, made
    by `load/real_server.py` and deleted when it stops. **Never** your cloud database;
  - counters, rate limits, the answer cache: an in-process Redis stand-in. **Never** Upstash;
  - the cloud vector store: only **read**, only the `documents_eval` collection (or the `_eval`
    collection named in `LOAD_COLLECTION`); every call that could write is refused in code;
  - the providers: free-tier quota (answers, triage, embeddings, rerank, Jev) on YOUR keys;
  - `load_out\` in the project folder (ticket, marker, made-up credentials, CSV files).
  Nothing has to be removed from any cloud store afterwards, because nothing is written there.
- **Needs (names only), in window A:** your provider keys (the settings of Run 12),
  `QDRANT_URL` and `QDRANT_API_KEY`, `LOAD_STEPS` and `LOAD_CONFIRM`. In window B: `LOAD_MODE`,
  `LOAD_STEPS`, `LOAD_CONFIRM`. **`DATABASE_URL`, `UPSTASH_REDIS_URL`, `EVAL_DATABASE_URL` must be
  unset in both windows**: the tools refuse to start with a remote one set, even though the run
  would not use it (the same guard as the evaluation). The collection must be `documents_eval`
  (Run 19) or another name that matches `^[a-z0-9]+_eval$`.
- **Step 1. The estimate (no call). It also makes the ticket:**

      uv run python scripts/load_estimate.py --steps 10x20,20x20

  You should see the plan, about how many questions it sends, the requests and tokens per
  provider against each provider's daily limit, then: `The ticket is for THIS plan and for today
  (UTC ...), is valid for 30 minutes from now, and works ONCE`, and the line `LOAD_MODE=real
  LOAD_STEPS=... LOAD_CONFIRM=CODE`. If the plan does not fit one day it says so and makes no
  ticket.
- **Step 2. Your decision.** Send me the estimate or decide yourself. Nothing starts without the
  ticket's code, and the code works once, today, within 30 minutes.
- **Step 3. Window A, the real server (it consumes the ticket when it is ready):**

      $env:LOAD_STEPS = "10x20,20x20"; $env:LOAD_CONFIRM = "CODE"
      uv run python load/real_server.py

  You should see `REAL PROVIDERS: ...` and `real server on http://127.0.0.1:8099 for the plan
  10x20,20x20 (20 accounts in a temporary SQLite file)`.
- **Step 4. Window B, the users, then the report:**

      $env:LOAD_MODE = "real"; $env:LOAD_STEPS = "10x20,20x20"; $env:LOAD_CONFIRM = "CODE"
      uv run locust -f load/locustfile.py --headless --host http://127.0.0.1:8099 --csv load_out/real --csv-full-history
      uv run python load/report.py load_out/real

- **If a line differs:** `refused: the ticket is older than 30 minutes` / `from another day` /
  `already used`: make a new estimate. `refused: ... DATABASE_URL points at a remote host`:
  unset it in that window. `refused: the collection is refused`: set `LOAD_COLLECTION` to an
  `_eval` collection. `no real server is running for this plan`: start step 3 first. Many
  `limited` in the report are the per-user rate limit and a free tier's own limit working; they
  are counted apart from failures. `unavail %` above 1 means the service answered "temporarily
  unavailable" (every provider limited or timed out): that IS a break.
- **Send back:** the estimate and the report.
- **Clear:** Ctrl+C in window A (the SQLite file goes); unset `LOAD_MODE`, `LOAD_STEPS`,
  `LOAD_CONFIRM` and the provider variables; delete `load_out\`.
- **Real calls:** about the number the estimate shows (about 5 per question), on your provider
  keys.
- **Safe to repeat:** only with a NEW estimate and ticket, and after the daily limits have reset
  (the estimate checks the same limits again, but it does not know what you used elsewhere
  today, and production traffic shares these keys).

## Run 28. First deploy to Render (needs your yes: real keys and accounts): PENDING

- **Verifies:** that the two services start on Render from `render.yaml`, that the client address
  the API counts is your real address, that the 512 MB host holds the masker and a login being
  hashed together, and that a new deploy is in the shadow stage.
- **Before it:** Runs 1 to 3 and 10 (the database, the collection, the cache and the documents
  exist), Run 24 (the secret scan is clean) and Run 25 if you want the recorded tests first. The
  migrations that are still pending (0004 and later, including `0010_withdrawn_documents.sql`)
  are applied by `docs/DEPLOY.md` 2.1 itself, so Runs 4, 8, 16 and 18 do not have to be done
  first for the schema; they are still needed for what they check (the first admin, the webhook).
- **Needs (names only):** the accounts of `docs/DEPLOY.md` section 1 and every secret of its
  table in section 5 (the "required" rows). You also fill in the region placeholder of
  `render.yaml`, and the admin's sign-in for 2.5a (a token from `Read-Secret` and
  `Invoke-RestMethod`; `curl.exe`, never `curl`, in Windows PowerShell).
- **Risk of the first public exposure (read before you click Deploy):** from the moment a service
  shows **Live** until 2.6 is finished, the address is public and the client address is Render's
  proxy, so every visitor shares ONE rate limit and ONE login throttle. A stranger who finds the
  address can use up the sign-in allowance and lock the admin out for a while (the throttle
  releases by itself; nobody can read data without an account). Do not publish or paste the
  address, and do 2.5 to 2.8 in one sitting. The service is in `shadow` (2.8), so no one but the
  admin sees an answer.
- **Steps:** follow `docs/DEPLOY.md` section 2.1 to 2.9 in order. The numbers to send back come
  from three of them:
  1. **2.5:** `curl https://YOUR-API.onrender.com/live` shows `{"status":"ok"}`.
  2. **2.6, the client address:** `render.yaml` ships `TRUSTED_PROXY_HOPS` at 0. `GET
     /admin/diagnostics` with the admin's token shows `address_at_one_hop`: compare it with your
     real public address from a "what is my IP" page. Only when they are equal, set
     `TRUSTED_PROXY_HOPS` to `1` in Render's Environment tab (and in `render.yaml`), save, and call
     the route again: `address_source` must now be `forwarded` and `client_address_used` your
     address.
  3. **2.7, memory:** read `/admin/diagnostics` right after a question was asked and a sign-in
     was done in a second tab: send `memory_mb` and `peak_memory_mb`.
  4. **2.8, the stage:** `GET /admin/shadow` shows `stage` `shadow`. If the database you use was
     not new, it may show another stage: set it back to `shadow` as 2.8 says, then send the stage
     you found.
- **You should see:** `/live` ok; the sign-in page; an admin who can sign in; the stage `shadow`
  on the Rollout page; the diagnostics numbers above.
- **If a line differs:** `address_at_one_hop` is not your address, or `forwarded_entries` is 0:
  leave the setting at 0, do not open the service, and send `forwarded_entries`,
  `trusted_proxy_hops`, `address_source` and the first two groups of `address_at_one_hop` only. `peak_memory_mb` over about 450: `DEPLOY.md`
  section 7 (lower `PASSWORD_HASH_CONCURRENCY`, then a larger instance; masking stays on).
  `refusing to start: NAME`: fix that variable.
- **Send back:** the four outputs, and the date and time the first deploy went Live. Then
  revoke the token (`POST /auth/logout`, 2.5a) and close the window.
- **Clear:** nothing to clear; do not move the stage out of `shadow` (DEPLOY.md section 6 is the
  checklist for that, a decision of its own).
- **Real calls:** a few (three to six questions to check it works).
- **Safe to repeat:** yes.

## Run 29. The rollback drill: PENDING

- **Verifies:** spec 17.3, "rollback by redeploying the previous build, in under 5 minutes,
  rehearsed once".
- **Before it:** Run 28.
- **Steps (with a stopwatch):**
  1. Make a harmless commit on `main` (for example a comment in `docs/runbooks/README.md`) and wait
     until Render shows the new deploy **Live**.
  2. Start the stopwatch. Render, `bfsi-api`, **Deploys**, the deploy before the last one,
     **Rollback**. Stop the stopwatch when `https://YOUR-API.onrender.com/live` answers ok.
  3. Ask one question in the frontend.
  4. **Check Auto-Deploy** of the service (Settings): Render may have switched it off by the
     rollback [T: verify and send what you saw]. Roll forward again: Deploys, the newest,
     **Rollback** to it, or switch Auto-Deploy back on and push a new commit. If a push does not
     deploy, it was off: write that down.
- **You should see:** the time under 5 minutes, `/live` ok, a question answered.
- **If a line differs:** more than 5 minutes: write what took the time (a cold start, a slow
  build) and send it; the runbook may need a different order.
- **Send back:** the minutes and seconds, the date, and whether Auto-Deploy was off after the
  rollback. Also write the time in `docs/runbooks/README.md` (the rehearsal column for "Bad
  deploy: go back").
- **Clear:** nothing.
- **Real calls:** one question.
- **Safe to repeat:** yes.

## Run 30. Qdrant snapshot, download and a test restore: PENDING

- **Verifies:** spec 11.3: whether the free cluster allows snapshots, that one can be taken and
  copied down whole, and that the copy can be restored. **Restoring a downloaded snapshot is
  UNPROVEN until this run has passed**; `docs/runbooks/restore-documents.md` says so. The
  restore goes into a throw-away collection called `documents_restore_test`, never into
  `documents`.
- **Before it:** Run 2 and Run 10 (a populated `documents` collection).
- **Needs (names only):** `QDRANT_URL`, `QDRANT_API_KEY`. The key must be allowed to create and
  delete collections (the test collection is created by the upload and removed at the end).
- **Step 1. See what is there (no change):**

      uv run python scripts/qdrant_snapshot.py list

  You should see `N snapshot(s) of 'documents'`, then one line per snapshot with its size, time
  and `copy here` or `no copy`.
- **Step 2. Make one and copy it down:**

      uv run python scripts/qdrant_snapshot.py create --run
      uv run python scripts/qdrant_snapshot.py download --newest --run

  You should see `created NAME (N bytes)`, then `saved NAME (N bytes, sha256 ...) in
  ...backups\qdrant`. A line `not saved: the size of the copy does not match the server's` means
  the download was cut short: nothing was kept; run it again.
- **Step 3. Restore the copy into the throw-away collection, count it, remove it:**

      uv run python scripts/qdrant_snapshot.py restore-test NAME --run

  (NAME is the name `saved` printed.) You should see `uploaded NAME into documents_restore_test`,
  then `points in documents_restore_test: N` and `points in documents: M`, then `removed
  documents_restore_test`. N equals M when nothing was ingested since the snapshot; a smaller or
  larger M is explained by what you ingested since, not by a fault. Without `--run` it only says
  what it would do. It refuses to start when `documents_restore_test` already exists (it never
  touches a collection it did not make) and it never writes to `documents`.
- **Step 4. If a line says `the cluster does not offer snapshots`** (exit code 2): the plan does
  not include them. Then the backup is a re-ingest; that route is the tested one anyway. (A line
  `the key was rejected`, exit code 3, is a different thing: check `QDRANT_API_KEY` and that it
  may make snapshots.) Do the restore test of `docs/runbooks/restore-documents.md` (one document
  into `documents_eval`, compare the active chunk counts) and fill in where `originals/` is
  backed up.
- **If a line differs:** `not started: QDRANT_URL is not set`: set it in this window. A different
  error prints its class name only: send me the class name and the step. If step 3 stops after
  the upload and `documents_restore_test` is left behind, run the same command again (it will
  say it already exists) and delete that collection in the console by hand: it is only a test.
- **Send back:** the output of steps 1 to 3 (names, sizes and counts), and the result of the
  `originals/` restore test of step 4. I then change the runbook from UNPROVEN to proven (or
  write what failed).
- **Clear:** unset the two variables; `backups\` is git-ignored.
- **Real calls:** about 6 requests to Qdrant, one of them a file upload of the snapshot's size.
  It uses the free cluster's disk for a few minutes (a second copy of the documents).
- **Safe to repeat:** yes.

## Run 31. A key rotation drill on a key you have a spare for: PENDING

- **Verifies:** `docs/KEY_ROTATION.md`: that you can replace a key and prove it, and what a save
  costs (about a minute of restart).
- **Before it:** Run 28.
- **Needs (names only):** the key you rotate (set it in this window with `Read-Secret`; never in
  a file) and a **spare** one at the provider. First check which names hold a value, so you pick
  a key that exists:

      uv run python scripts/key_inventory.py

  Each line shows the setting name, `keys` (how many it holds) and short fingerprints. Pick the
  first row of this table whose setting shows a count of 1 or more AND for which you can make a
  new key at the provider without disturbing anything else:

  | Pick | Setting | How the drill goes |
  |---|---|---|
  | 1 | `GROQ_API_KEYS` (a list, up to four) | add the new key after a comma, save, check, revoke the old one at Groq, remove it from the list, save |
  | 2 | `GEMINI_EMBEDDING_KEY_B` (a single value, a rotation overlap of the same account) | replace it, save, check, revoke the old one |
  | 3 | `GEMINI_API_KEYS` or `OPENROUTER_API_KEYS` (lists) | as pick 1 |

  `GEMINI_EMBEDDING_KEY_B` is optional: if its count is 0, use pick 1. Do **not** drill with
  `GEMINI_EMBEDDING_KEY_A`, `JWT_SECRET_KEY`, `AUDIT_HASH_KEY` or `LANGFUSE_USER_HASH_KEY`
  (their rotation has side effects: `docs/KEY_ROTATION.md`).
- **Steps:** follow "The order" in `docs/KEY_ROTATION.md` exactly, with
  `uv run python scripts/key_inventory.py` before and after. The inventory reads only the
  variables of THIS window, so type the key lists into the window (`Read-Secret`) before each
  run; the fingerprints then show the keys you typed, not what Render holds. What proves Render
  holds the new key is a question answered and `/health` healthy. Each save on Render restarts
  the service: write down how long it took.
- **You should see:** the old fingerprint gone from your window's list, the new one present, the
  count the same as before once the old key is removed, no `WARNING: the same key ...` line,
  `/health` healthy, a question answered.
- **If a line differs:** the old fingerprint is still there: the old key is still in the list;
  remove it and save.
- **Send back:** the two inventory outputs (names, counts and fingerprints), which pick you used,
  and the minutes it took.
- **Clear:** unset the variables in your window (`Remove-Item Env:NAME`) or close it. If the key
  is also in a scheduled task's encrypted file (only `QDRANT_*` and `DATABASE_URL` are), replace
  that file too (`docs/DEPLOY.md` 9.1).
- **Real calls:** one question.
- **Safe to repeat:** yes.

## Run 32. The database connection count on the deployed service: PENDING

- **Verifies:** how many connections the API holds, against the plan's limit, before anyone
  considers the pooled address (`docs/DEPLOY.md` section 4).
- **Before it:** Run 28.
- **Needs (names only):** `DATABASE_URL` (the direct address, in this window only).
- **Steps:** open the frontend in four browser tabs and ask a question in each at about the same
  time, then at once run:

      uv run python scripts/db_connections.py

- **You should see:** `connections open now: N of M`, the states, and the share of the limit.
  Repeat it three times during use and note the highest.
- **If a line differs:** `refused: this is a SQLite address`: the shell holds the wrong address.
- **Send back:** the highest `N of M` and the share. No decision about the pooled address is made
  from this alone: it needs its own deliberate test.
- **Clear:** unset `DATABASE_URL`.
- **Real calls:** the questions of the tabs.
- **Safe to repeat:** yes.

## Run 33. The scheduled tasks on this machine: PENDING

- **Verifies:** that retention, the weekly drift report and the weekly snapshot run by themselves
  from Windows Task Scheduler, and that the `retention_overdue` alert stays quiet.
- **Before it:** Runs 22 and 30 for the drift and snapshot tasks; the encrypted secret files of
  `docs/DEPLOY.md` section 9.1.
- **Steps:** create the three tasks of the table in section 9 (three small scripts, each writing
  its output to its own log file under `bfsi-tasks`) and run each once by hand from Task
  Scheduler (**Run**). Read each log and **Last Run Result** (0 is success).
- **You should see**, line by line (the program's own wording):
  - **retention.log:** `deleted N chats (M messages)`, then `deleted A audit rows, B alert rows
    and C hourly metric rows older than 30 days`, then either `deleted S masked evaluation
    samples older than 30 days` or, when `EVAL_DATABASE_URL` is not set, nothing, or, when the
    second database failed, `evaluation samples: not purged (ClassName); the chat and audit parts
    above are done` (exit 0 all the same). Optionally `traces: N chats queued for removal ...`.
    N, A, B, C are usually 0 on a young system. Each run writes the `retention_sweep` row.
  - **drift.log:** `production: N sample(s) this week, M in the baseline window`, one line per
    measure (`name: PSI x, JS y (level)`), then `alerts: none` (or the names of the alerts).
    It also saves one drift row in the evaluation database (that is what the page shows).
  - **snapshot.log:** `created NAME (N bytes)`, `saved NAME (N bytes, sha256 ...) in ...`, then
    `K snapshot(s); keeping the newest 5; deleting D` (D is 0 until more than five exist) and
    one `kept NAME: no downloaded copy` line for each older snapshot you never downloaded, and
    `deleted NAME` for any it removed. Exit 0.
- **If a line differs:** `not started: NAME is not set in this window`: the encrypted file of that
  name is missing. `the key was rejected` or `the cluster does not offer snapshots`: Run 30.
- **Send back:** the three outputs (counts and names only) and the date the tasks were created.
  A week later check the admin banner has no `retention_overdue` (the alert waits seven days).
- **Clear:** the encrypted files stay (they are yours); remove a task you no longer want.
- **Real calls:** a few requests to the database, the cache and Qdrant; no model call.
- **Safe to repeat:** yes.

## Run 34. Migration 0010 and the withdrawn-document record (needs your explicit yes): PENDING

- **Verifies:** that the new table exists on the real database, that a delete records the id and
  moves the PDF, that an ingest of that id is refused, and that after a "restore" (here: putting
  the vectors back) the `withdrawn` command finds the document and one more delete clears it.
  It works on the test collection `documents_eval` and a local SQLite file, never on
  `documents` or the cloud database. (The cloud database gets the table from the migration in
  `docs/DEPLOY.md` 2.1.)
- **Before it:** Run 19 (the test collection and the sample documents; its local evaluation
  SQLite file).
- **Needs (names only):** `QDRANT_URL`, `QDRANT_API_KEY`, `UPSTASH_REDIS_URL` is **not** needed
  (a test collection never touches the cache), `DATABASE_URL` set to the local SQLite file of
  Run 19 (a `sqlite:///...` address; the command refuses a remote one for a test collection), the
  Gemini embedding key of Run 19 for the ingest.
- **Step 1. Apply the migration to the local file:**

      uv run python -m app.db.migrate

  You should see `0010_withdrawn_documents.sql` applied (and nothing else pending).
- **Step 2. Ingest one sample document into the test collection under a throw-away id, as Run 19
  does** (use its exact command with `--doc-id withdraw-test --collection documents_eval`). You
  should see `new N ... version 1` and `original` kept in `originals\withdraw-test\`.
- **Step 3. Delete it:**

      uv run python -m app.ingest delete --doc-id withdraw-test --reason other --collection documents_eval

  Type `withdraw-test` when asked. You should see `removed N chunk(s) ...`, then `recorded as
  withdrawn (reason: other); 1 original PDF(s) moved`. The PDF is now under
  `originals\_withdrawn\withdraw-test\`.
- **Step 4. Try to bring it back by accident:** repeat step 2. You should see an error that the
  id `was withdrawn` and names `--reinstate`; nothing is ingested and no PDF appears in
  `originals\withdraw-test\`.
- **Step 4b. The safeguards of `--reinstate` (this is the only run that uses it):**
  1. Repeat step 2 with `--reinstate withdraw-test` and **no** `--confirm-id`: it must refuse
     (`--reinstate needs the document id twice`), nothing is ingested.
  2. Repeat step 2 with `--reinstate withdraw-test --confirm-id withdraw-test`: you should see
     `'withdraw-test' was withdrawn: its record is removed (--reinstate)` and the ingest goes on.
  3. Run step 3 again: it records the id again, with a new time.
  A routine drill never needs `--reinstate`: use a throw-away id.
- **Step 5. The list:**

      uv run python -m app.ingest withdrawn --collection documents_eval

  You should see `1 withdrawn document id(s)` and a line for `withdraw-test` ending `gone`,
  exit code 0.
- **Clear:** delete the throw-away document's folder `originals\_withdrawn\withdraw-test\`;
  unset the variables. The record stays in the local file, which is only a test file.
- **If a line differs:** `no such table: withdrawn_documents` means step 1 did not run on the
  file `DATABASE_URL` points at. A different error prints its class name: send it.
- **Send back:** the output lines of steps 3 to 5.
- **Real calls:** a handful of Qdrant requests and one embedding call (the ingest).
- **Safe to repeat:** with a new throw-away id each time (an id stays withdrawn).

## Run 35. The red-team set (needs your yes: real evaluation keys): PENDING

- **Verifies:** that the system holds against the 40 adversarial cases of
  `app/eval/data/redteam.json`: injection, role escalation, out-of-domain and jailbreak phrasings,
  and PII in the question. Two halves of 20: **development** (to find and fix weaknesses) and
  **held-out** (the only half the pre-launch gate is judged on).
- **Before it:** Run 19 (the test collection and its documents), Run 20 (calibration) and Run 21
  (the baseline, so that a red-team result has something to be read against). The 60-case set is
  not touched.
- **Needs (names only):** the evaluation keys of Run 20 and the settings of Run 19, in this
  window only. A red-team run is a normal evaluation run: it shows an estimate first and starts
  only with its own confirmation code.
- **Step 1. Check the file (no call):**

      uv run python -m app.eval check

  You should see `red-team set: 40 cases (development and held-out halves of 20)` and no
  `problem:` line.
- **Step 2. The development half.**

      uv run python -m app.eval estimate --set redteam_dev

  You should see `run RUN_ID (redteam_dev, local, 20 cases)`, the estimate, and the line
  `To start it, type the confirmation code: run RUN_ID --confirm CODE`. Your decision is yes or
  no; then `uv run python -m app.eval run RUN_ID --confirm CODE`.
- **Step 3. Read the development result, fix what it shows, and only then the held-out half.**
  Fixing against the development half is the point of it. **A fix to the system and a change to
  a red-team case are never made in the same week** (spec 15.1).
- **Step 4. The held-out half, once, after the fixes:**

      uv run python -m app.eval estimate --set redteam_heldout

  then `run RUN_ID --confirm CODE` as above. Do not fix against this half and run it again the
  same day: that turns it into a second development half. The pass threshold is YOUR number,
  written in `docs/prelaunch_evidence.json` (`red_team.threshold`) before you look at the result.
- **You should see:** the development half case by case; the held-out half as a count (the
  admin page shows `failed_held_out_count` and hides the ids). Any `pii_leak`, cross-role
  leak or `prompt_leak` is an alert whatever the pass rate (the zero rules of
  `docs/TARGETS.md`).
- **If a line differs:** `the red-team set is broken: ...` names the rule (a duplicated question,
  a wrong half, a case outside the sample documents): send it, do not edit the file yourself in
  the same week as a system change.
- **Send back:** both run ids; for the development half the number passed and the ids of the
  cases that failed; for the **held-out half the number passed and nothing else**: no case
  id, no answer, no wording. The admin page and `docs/TARGETS.md` show held-out failures as
  a count only, so that nobody tunes against that half by accident.
- **Clear:** unset the evaluation keys.
- **Real calls:** about the number the estimate shows (the free-tier limits of ONE key per
  provider, `docs/READINESS.md`); split over days if it does not fit one.
- **Safe to repeat:** the development half yes (a new estimate and code each time); the held-out
  half only as a final check, not as a tuning loop.

## Run 36. The security scan: PENDING

- **Verifies:** spec 17.6 "no open critical or high findings". `docs/SECURITY_SCAN.md` says what
  each tool covers and why each accepted finding kind is accepted.
- **Before it:** nothing. No key, no service: it reads the repository and the package index.
- **Step 1. Audit the files CI does not audit (on your machine):**

      uvx --from pip-audit==2.10.1 pip-audit -r requirements.txt -r requirements-dev.txt
      uvx --from pip-audit==2.10.1 pip-audit -r requirements-frontend.txt
      uvx --from pip-audit==2.10.1 pip-audit -r requirements-ingest.txt
      uvx --from pip-audit==2.10.1 pip-audit -r requirements-eval.txt

  You should see `No known vulnerabilities found` for each, apart from the skipped wheel
  `en-core-web-sm` (not on PyPI). The two accepted advisories of the eval file are listed in
  `docs/SECURITY_SCAN.md`; anything else is OPEN until you decide.
- **Step 2. The static scan against the accepted baseline:**

      uvx bandit==1.9.4 -r app scripts load frontend -q -f json -o bandit_now.json
      uv run python scripts/bandit_check.py bandit_now.json

  You should see `N finding(s) in the scan, N accepted, 0 new` and exit code 0 (bandit itself
  exits 1 when it finds anything; that is expected). A new finding is printed with its file and
  line: read it, then fix it, or accept it by rebuilding the baseline on purpose (the commands are
  in `docs/SECURITY_SCAN.md`). Delete `bandit_now.json` afterwards.
- **Step 3. Every place SQL text is built from parts (no service):**

      uv run python scripts/sql_sites.py --check

  You should see `32 site(s) found, 32 reviewed, 0 new`. A new site is printed with its file
  and line: read it, and add it to `docs/sql_sites.json` only if it puts nothing but a name, a
  run of placeholders or a fixed fragment into the text.
- **Step 4. Secrets (Run 24):** `gitleaks git --redact --no-banner --verbose`.
- **Send back:** the three outputs' last lines and the counts of open critical and high findings.
  Record them in `docs/prelaunch_evidence.json` (`security_scan`: `scans` with the three names,
  `critical_open`, `high_open`, and an `evidence` citation such as `date:YYYY-MM-DD`).
- **Real calls:** requests to the package index only.
- **Safe to repeat:** yes.

## Run 37. Numeric targets from the baseline (after Run 21): PENDING

- **Verifies:** that the baseline turns into the targets of `docs/TARGETS.md` by the four rules
  you approved, and that no number is set without a measurement.
- **Before it:** Run 21 (three unchanged runs of the active set: the variance band).
- **Step 1. Read the proposals (no call, writes nothing):**

      uv run python -m app.eval targets

  You should see `baseline of 3 runs of dataset 2026-10-v1`, then one line per target with its
  rule, the baseline, the band, the signed floor, the **proposed** target and when it alerts.
  Without three finished runs it says `no baseline yet` and every target stays "baseline".
- **Step 2. Sign the absolute floors.** For citation validity, numeric faithfulness,
  faithfulness, refusal correctness and access isolation, choose a number independent of the
  baseline and add it to `app/eval/data/targets_signed.json` with your name and the date:
  `{"absolute_floors": {"citation_validity": {"floor": 0.95, "signed_by": "NAME", "date":
  "YYYY-MM-DD"}}}` (the 0.95 is only the shape). A number without a name and a date is ignored.
- **Step 3. Record each decision** in the status column of `docs/TARGETS.md`, and the starting
  values of its section C that the baseline replaced.
- **Send back:** the output of step 1 (numbers only).
- **Real calls:** none. **Safe to repeat:** yes.

## Run 38. The cost projection against a dry run: PENDING

- **Verifies:** spec 17.6 "a cost projection compared with a dry run".
- **Before it:** Run 21 or Run 27: you need a MEASURED tokens-per-query (the `tokens_per_query`
  line of Run 37, or the report of the real load run).
- **Step 1.**

      uv run python scripts/cost_dry_run_compare.py --queries-per-day 100 --measured-tokens-per-query MEASURED

  You should see the projected and the measured tokens per query, the ratio, and whether it is
  within the 25 % margin. Outside it, the placeholder sizes of `app/eval/estimate.py` are
  replaced by the measured ones (a code change, with a release record).
- **Step 2.** Record the ratio in `docs/prelaunch_evidence.json` (`cost_projection.ratio`) with
  an `evidence` citation. The queries per day stays "to be named by the owner" until you name it.
- **Real calls:** none. **Safe to repeat:** yes.

## Run 39. The pre-launch gate and the readiness score: PENDING

- **Verifies:** the nine items of spec 17.6, in one report, and the stage checklists of spec 19.
- **Before it:** Runs 21, 26, 29 and 35 to 38 as far as you have done them. The gate is honest
  about what is missing: it prints NO EVIDENCE for it.
- **Step 1. Fill in what you have.** After each run write its result in
  `docs/prelaunch_evidence.json` (`status` pass or fail, **`date`: the day you checked it
  (YYYY-MM-DD)**, a citation, the numbers the item needs) and score the lines of
  `docs/READINESS.md` **with a citation each**, and write the day on its `Scored on` line; a
  line with no citation scores 0. Score conservatively. A record older than 30 days is
  `STALE` (7 days for the two stage items that say "last 7 days"): check it again and
  re-record it. The file is typed by you: the gate refuses an empty or inconsistent claim,
  it cannot prove a true one.
- **Step 2. The report (no call):**

      uv run python scripts/prelaunch_gate.py --write

  You should see PASS, FAIL or NO EVIDENCE for each of the nine items with the reason, `Alert
  only: nothing is blocked by this report`, and `saved YYYY-MM-DD-prelaunch-gate.md`. Attach that
  file to the release record. The exit code is 0 whatever it says.
- **Step 3. A stage checklist:**

      uv run python scripts/prelaunch_gate.py --stage staff_beta

  (and `--stage open`). Moving the stage is still your deliberate step in the admin page.
- **Step 4. The second review.** A second reviewer reads each cited item and re-scores
  `docs/READINESS.md`; fill in the "Second review" line. **An independent second review is also
  required before any real customer data** (`docs/ACCESS_CONTROL_AUDIT.md`).
- **Send back:** the saved report (it holds names and numbers only).
- **Real calls:** none. **Safe to repeat:** yes.

## Run 40. One conversation per chat (Change round 1, step 1): PENDING

- **Verifies:** that a first question gets its conversation id before it is saved, so the next
  question joins the same chat even if the save is slow or fails (before this, every question
  could start a new chat: 13 chats for 15 questions in the daily table of 2026-10-12).
- **Before it:** Run 16 (the API and the frontend run locally against your own Neon database).
- **Needs:** the settings of Run 16, in the same window. No new variable.
- **Step 1.** Start the API and the frontend as in Run 16. Sign in as a customer and click
  **New chat** once.
- **Step 2.** Ask three questions one after the other, in the same window, without clicking
  anything else (for example: a question about a waiting period, a follow-up "and for accidents?",
  and a third about premiums).
- **You should see:** all three questions and answers stay on the screen in one conversation, and
  the left column "My chats" lists exactly **one** new chat. Click **New chat** and ask one more
  question: now "My chats" lists two chats.
- **If it differs:** more chats than "New chat" clicks means the id is still lost. Send back the
  number of chats and the number of questions, and the line `persist_queued` if it shows in the
  Alerts screen (an event name and a count; no text).
- **Send back:** chats listed, questions asked, whether `persist_queued` appeared.
- **How to clear:** close the windows. The test chats can be deleted in "My chats".
- **Real calls:** about 4 answers (a few free-tier model calls each). **Safe to repeat:** yes.

## Run 41. The key panel: migration 0011, calls today and calls left per key: PENDING

- **Verifies:** that the gateway counts every provider call on the key that made it, that the
  counts survive a restart, and that the admin Dashboards screen shows the number of keys, the
  calls and tokens made today, and the calls left of the daily limit, for each key.
- **Before it:** Run 40, and the migrations up to 0010 applied (Runs 1 to 8 and later).
- **Needs:** `DATABASE_URL` (the direct Neon address, `sslmode=require`) in the window.
- **Step 1. Apply migration 0011 (adds one small table, `key_usage_daily`):**

      uv run python -m app.db.migrate --dry-run
      uv run python -m app.db.migrate

  You should see `0011_key_usage_daily.sql` as pending, then applied. It holds a day, a pool name,
  a fingerprint (eight characters of a hash) and two counts; no key and no text.
- **Step 2. Check how your key variables are read (no call, prints counts and fingerprints):**

      uv run python scripts/key_inventory.py

  A variable written as `key1 , key2, key3` must show three keys. Note the fingerprints: the
  dashboard uses the same names.
- **Step 3.** Start the API with your usual settings, sign in as an admin, ask two questions in a
  chat, then open **Dashboards, Key pools**.
- **You should see:** the metric "Keys in all pools"; a table per provider (state, keys, cooling
  down, calls today, tokens today, daily calls per key, daily tokens per key); and a table "Each
  key, today" with one row per key: its position, fingerprint, calls today, calls left today,
  tokens today and tokens left today. The two questions show as a few calls on the answer
  providers and on Jev, embedding and rerank. "not set" means that provider has no configured
  daily limit.
- **Step 4. The counts survive a restart.** Wait five minutes (the counts are saved every five
  minutes), stop the API with Ctrl+C (it saves once more), start it again and open the panel: the
  calls made before the restart are still there.
- **If it differs:** all counts stay at 0 after questions: send back the provider names and the
  `observability_failed` count of the Alerts screen. A fingerprint that does not match
  `key_inventory.py`: send both lists (fingerprints only).
- **Send back:** the output of step 1 and 2 (counts and fingerprints only), and for step 3 the
  numbers of one provider row.
- **How to clear:** close the window. The table can be left as it is: it holds a few rows a day.
- **Real calls:** about 2 questions. **Safe to repeat:** yes.

## Run 42. Alert reasons on the Alerts screen (Change round 1, step 3): PENDING

- **Verifies:** that every alert and event on the Alerts screen has a "View reason" box with a
  fixed plain-words text (what it means, what to do), and that the reason codes seen are class
  names only (never a message, an address or a key).
- **Before it:** Run 18 (the Alerts screen works).
- **Needs:** the settings of Run 16. No new variable.
- **Step 1.** Start the API and the frontend, sign in as an admin, open **Alerts**.
- **You should see:** under "Alerts of the last 24 hours" a column "what it means" and, below the
  table, one "View reason: NAME" box per alert; under "Events seen by this running service" a
  column "reason codes" and one box per event. Open one box: "What it means", "What to do", and
  for an event "Reason codes seen" (for example `TimeoutError (3)`).
- **Check a known one:** if `retention_overdue` is shown, its box must say that the clean-up task
  has not run and name the command; run `uv run python -m app.memory.retention --dry-run` as it
  says (it only lists what it would delete).
- **If it differs:** a box that says "No further description has been written for it": send back
  the event's name (a code names it; no text). A reason that contains an address, a key or a
  sentence you did not expect: stop and send back the box's title only.
- **Send back:** the number of alerts and events shown and whether each had a box.
- **Real calls:** none. **Safe to repeat:** yes.

## Run 43. The Delete button of the Documents screen (Change round 1, step 4): PENDING

- **Verifies:** that the Delete button beside a document runs the delete command on your machine,
  after you type the document id, with the same safeguards as the command in your terminal.
- **Before it:** the Run 8 to 12 ingest steps work. The drill uses a **throw-away id**, never a
  real one: **a delete records the id as withdrawn for good**, so a restore cannot bring it back.
  Use `rehearsal-button-1`, then `rehearsal-button-2` the next time.
- **Needs:** the settings of Run 16 plus `QDRANT_URL`, `QDRANT_API_KEY` and the embedding keys
  for the ingest (as in Run 12); the frontend started with `ADMIN_MACHINE=true`; the API started
  from this same window (the button runs the command with this window's settings).
- **Step 1. Ingest a throw-away copy of a sample (a command, as always):**

      uv run python -m app.ingest ingest spec/samples/company/insurance_flexi_commercial_property.pdf `
        --doc-id rehearsal-button-1 --doc-type policy --access-role customer --product-line insurance

  You should see its chunks counted and indexed.
- **Step 2.** Open **Documents** in the frontend. You should see a row for `rehearsal-button-1`
  with its access role, active chunks and a **Delete** button beside it. The other documents have
  one too: do not press those.
- **Step 3.** Press **Delete** on `rehearsal-button-1`. A red box asks "Delete this document?".
  The button **Delete permanently** is grey. Type `rehearsal-button` (wrong): it stays grey.
  Type the whole id: it turns on. Pick a reason and press it.
- **You should see:** after up to a minute, "Document deleted" and the command's own lines
  (`rehearsal-button-1: removed N chunk(s), ...` and `recorded as withdrawn`). The row is gone
  from the list.
- **Step 4. Check it (your terminal):**

      uv run python -m app.ingest status --doc-id rehearsal-button-1
      uv run python -m app.ingest withdrawn

  `status` shows `stored 0`; `withdrawn` lists the id, with the reason you picked and `gone`.
  In **Alerts** there is no `document_delete_failed`. In the audit log (Neon) one row
  `document_delete_request` (who, the document id, the reason code) and one row `delete`.
- **If it differs:** "The delete did not finish": press Delete again for the same id (safe to
  repeat) and send back the lines it printed. A timeout after 150 seconds: run the command of
  Run 12 (f) in your terminal instead.
- **Check the host refuses it:** on the deployed API (Render) the button does not appear (the
  frontend is not on the admin machine) and a direct call is refused with `admin_machine_only`.
- **Send back:** the lines shown after the delete (names and counts only) and the `status` line.
- **How to clear:** nothing: the document is gone. Close the windows.
- **Real calls:** about 3 embedding calls for the ingest; the delete makes none. **Safe to repeat:**
  yes, with a new throw-away id each time.

## Run 44. The purge of all data (Change round 1, step 6): PENDING

- **Verifies:** that `python -m app.maintenance purge` and the Maintenance screen's button show what
  would be erased, ask for the phrase, and (only when you go on) erase the right things and keep
  the right things.
- **IRREVERSIBLE.** The real purge erases the document vectors, the cached answers, every chat,
  the customer and staff accounts, the review queue, the alert rows and the metrics of the stores
  in YOUR window. Do steps 1 and 2 freely. Do step 3 only on data you are willing to lose
  (a rehearsal database of your own, or a real reset on purpose). It keeps the admin accounts, the
  audit log, the allow-list entries, the withdrawn-document list and the settings.
- **Before it:** Runs 3 and 6 (the stores and an admin account work). Take a Qdrant snapshot first
  if you may want the documents back (Run 28 gives the command).
- **Needs:** `DATABASE_URL` (direct Neon address), `QDRANT_URL`, `QDRANT_API_KEY`,
  `UPSTASH_REDIS_URL` in the window (set them as in Run 6). The command always works on the
  `documents` collection and cannot be pointed at another.
- **Step 1. The dry run (changes nothing):**

      uv run python -m app.maintenance purge --dry-run

  You should see `would be erased:` with a count for each kind (document chunks, chats, messages,
  thumbs, accounts, review items, alert rows, metric rows, counter rows), `and every cached answer`,
  `kept: admin accounts; the audit log; the allow-list entities; ...` with your number of admins,
  `not touched: Langfuse traces, the evaluation database, the original PDF files`, then
  `dry run: nothing was changed`. Exit code 0.
- **Step 2. The gates (changes nothing):**

      uv run python -m app.maintenance purge

  It prints the counts and the warning, then asks `Type DELETE ALL DATA to confirm:`. Type `no` (or
  anything else): it must say `not confirmed; nothing was erased` and exit 1. Run the dry run again:
  the same counts.
- **Step 3. The real purge (IRREVERSIBLE).** Only if you mean it: run the same command and type the
  phrase exactly. You should see `erasing the document vectors`, `clearing the cached answers`,
  `erasing the database rows`, an `erased: ...` line with the counts, and `done`. Then:
  - the dry run shows zeros and the same number of admins;
  - sign in again as the admin: it works; the Documents screen is empty; "My chats" is empty;
  - in Neon, the audit log has a row `purge_all` with the counts (and no names or text).
  Ingest a document again (Run 12): it works without any set-up.
- **Step 4. The button.** With the frontend started with `ADMIN_MACHINE=true` and the API started
  in the same window, open **Maintenance**, press **Purge Database Data**: a red panel shows the
  counts and the warning; **Erase everything** is grey until you tick the box AND type the phrase.
  Press **Cancel** (do not erase unless you mean it). On the deployed (Render) service the screen
  only says that erasing is done on the admin machine, and a direct call is refused with
  `admin_machine_only`.
- **If a line differs:** `purge incomplete: run the same command again`: run it again (it is safe to
  repeat) and send back the lines it printed. A store named in `incomplete` shows which one failed.
- **Send back:** the dry-run output of step 1 (counts only) and, if you did step 3, the `erased:`
  line and the `done` line.
- **How to clear:** close the window.
- **Real calls:** the dry run reads counts from your real stores; the real purge deletes. No model
  call, no key of a provider. **Safe to repeat:** the dry run yes; the real purge yes (a second run
  finds nothing to erase), but it can never be undone.

## Run 45. The Dark mode switch (Change round 1, step 7): PENDING

- **Verifies:** that the "Dark mode" switch in the sidebar draws the whole page in the dark or the
  light colours whatever theme your browser shows, and that no screen is left half dark.
- **Before it:** Run 17 (the frontend runs and you can look at every screen).
- **Needs:** nothing new.
- **Steps.** Start the API and the frontend as in Run 17 and sign in as an admin. The switch is
  under **New chat**. It starts to match your browser's theme.
  1. Switch it the other way. The page background, the sidebar, the chat cards, the notices and
     the chips must change at once.
  2. Open each screen in turn with the switch ON and again with it OFF: Chat, Documents, Entities,
     Review queue, Users, Alerts, Dashboards, Rollout, Evaluation, Maintenance. Look for text you
     cannot read, a white box on a dark page (or a dark box on a light page), a table that is hard to
     read, a drop-down list that is the wrong colour, and a button you cannot see.
  3. Reload the page: the switch goes back to matching your browser (it is remembered for this tab
     only).
- **Known limit.** Streamlit's own widgets (inputs, buttons, drop-downs, tables) take their colours
  from Streamlit's theme, which no code can change. When the switch picks the OTHER theme the page
  repaints them with CSS rules that follow Streamlit's markup (tables are inverted). If one looks
  wrong in your Streamlit version, send back the screen's name and which theme: it is a CSS fix.
- **Send back:** for each screen and theme: fine, or a short note of what looks wrong.
- **Real calls:** none. **Safe to repeat:** yes.

---

# C. Added by later phases

Rule (also in `CLAUDE.md` and the build-phase skill): every phase that has a real-service check
appends its runs here, in the same shape as above (number, name, what it verifies, before,
needs, commands, what you should see, what to do if a line differs, what to send back, how to
clear, approximate calls and limits, safe to repeat), **before the phase can be called
closed.** Also add the run to the table at the top.

Known candidates, not yet written (the phase that creates the code writes the run):

- Phase 9 (cache hardening): written above (Runs 14 and 15).
- Phase 10 (chat API): written above (Run 16).
- Phase 11 (Streamlit): written above (Run 17).
- Phase 12 (observability): written above (Run 18).
- Phase 13 (evaluation): written above (Runs 19 to 23; the ones with real keys need your yes).
- Phase 16 (baseline): written above (Runs 35 to 39): the red-team set, the security scan,
  the targets from Run 21's numbers, the cost comparison and the pre-launch gate.
- Phase 14 (load tests): the load run, with the free-tier limits it may hit.
- Phase 15 (deployment): the Render deploy steps and the proxy-header test.
