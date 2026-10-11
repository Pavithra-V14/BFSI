# Owner runs: everything you run yourself with real keys and real services

Claude never runs any of these. You run them in your own PowerShell window, in the project
folder, and you send only the short "send back" lines (names, counts and status; never a key, a
URL or a piece of text) to your advisor.

Last updated: 2026-10-10 (after Phase 8). Each phase that has a real-service check adds its runs
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

| # | Run | Verifies | Status |
|---|---|---|---|
| 1 | Apply migrations 0001 to 0003 | Phase 3 | DONE |
| 2 | Create the Qdrant `documents` collection (`--ensure`) | Phase 3 | DONE |
| 3 | Upstash counters and corpus-version check | Phase 3 | DONE 2026-10-06 |
| 4 | Make the two keys, apply migration 0004 | Phase 4 | DONE |
| 5 | Create the first admin | Phase 4 | DONE |
| 6 | Run the API locally and check it | Phase 4 | DONE |
| 7 | Real-Qdrant check on a throw-away collection | Phase 5 | DONE |
| 8 | Apply migration 0005 | Phase 7 | PENDING (stays pending until you tell me it is applied) |
| 9 | Gateway smoke test with real keys | Phase 2 | PENDING |
| 10 | Phase 5 real ingest (steps a to h, and i: the staff-only copy) | Phase 5 | PENDING |
| 11 | The IsEmpty filter on real chunks | Phases 3 and 5 | PENDING |
| 12 | Pipeline smoke, one real question per role (12b: the role-isolation test) | Phase 8 | PENDING |
| 13 | Pipeline smoke with Langfuse | Phase 8 | PENDING |
| 14 | Real-Upstash check of the cache code (`cache_check.py`) | Phase 9 | PENDING |
| 15 | Refresh sparse vectors (only needed after a tokenizer change) | Phase 9 | PENDING |
| 16 | Local end-to-end chat through the API (migrations 0006 and 0007, shadow mode, sessions, feedback) | Phase 10 | PENDING |
| 17 | Look at every screen of the Streamlit frontend, with the API running | Phase 11 | PENDING |
| 18 | Observability: migration 0009, the webhook, the log scan, trace deletion on real Langfuse, the purge and the dashboards | Phase 12 | PENDING |

Order matters for the pending runs: 8, then 9, then 10, then 11, then 12, then 13. Runs 9 and 10
use up free-tier calls, so do them on a day when you have not used the providers much.

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

    uv run python -m app.ingest delete --doc-id insurance-flexi-property

It asks you to type the id.

- **You should see:** `insurance-flexi-property: removed 19 chunk(s), N allow-list entr(ies); 0
  chat message(s) flagged 'source removed'`. Then `status` shows `stored 0`,
  `allow-list entries: none`, marker `no`. Then ingest it again (the command in (b)): `new 19
  ... version 1`, much quicker and with far fewer model calls (the answers are saved in
  `originals/.cache`).

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

1. `uv run python -m app.ingest delete --doc-id banking-rtl-facility` (type its id).
2. `Remove-Item -Recurse -Force originals\.cache` (so the picture is asked for again).
3. Break the Gemini key for this window only: `$env:GEMINI_API_KEYS = "not-a-real-key"`
4. Run the banking ingest command again.
   - **You should see:** `document 'banking-rtl-facility' was NOT activated; the old version
     (if any) stays active`, one or more `page N failed (<an error name>)` (at least page 2,
     which holds the picture), and `each failed page is in the review queue; run the same
     command again to retry`. Exit code 2.
5. `status`: `stored 0` and `failed pages waiting in the review queue:` as many as there were
   `page N failed` lines.
6. Put the real key back (`$env:GEMINI_API_KEYS = Read-Secret "GEMINI_API_KEYS"`), run the same
   ingest again: `new 22 ...`, exit 0, and `status` shows `failed pages waiting ... : 0`.

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
- **Before it:** Run 10 with step (i) (the staff-only copy `ecas-staff-only` is ingested, 21
  active chunks), and the three main questions of Run 12 done (they need `finserv-mf-ecas`).
- **Needs:** the variables of Run 10 (for `ingest`, `status` and `delete`) and of Run 12 (for
  the smoke runs), set in the same window.
- **Why the customer copy goes first:** the staff-only copy holds the same text as
  `finserv-mf-ecas`, which a customer may see. While both exist a customer would be answered
  from the customer copy and the test would prove nothing. So the customer copy is deleted for
  the test and put back at the end. (The ids are only labels: the program separates the two
  copies by id and role.)
- **The question** is one only the eCAS text can answer. It is not in the insurance or banking
  samples: *Which folio is pending nomination opt-in or opt-out?*

**Step 1. Take the customer copy away.**

    uv run python -m app.ingest delete --doc-id finserv-mf-ecas

It asks you to type the id.

- **You should see:** `finserv-mf-ecas: removed 21 chunk(s), N allow-list entr(ies); 0 chat
  message(s) flagged 'source removed'`. Then
  `uv run python -m app.ingest status --doc-id finserv-mf-ecas` shows `stored 0` and
  `allow-list entries: none`; `status --doc-id ecas-staff-only` still shows `active 21`.

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

**Step 6. Put the customer copy back.**

    uv run python -m app.ingest ingest spec/samples/company/finserv_mutual_fund_ecas_and_servicing.pdf `
      --doc-id finserv-mf-ecas --doc-type policy --access-role customer --product-line finserv

- **You should see:** `finserv-mf-ecas: new 21, changed 0, unchanged 0, removed 0, version 1`,
  exit code 0 (quick: the answers are cached). Then `status --doc-id finserv-mf-ecas` shows
  `active 21`.

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

- **Verifies:** Phase 8 (the Langfuse sink sends span records only, and a failure never touches
  an answer).
- **Before it:** Run 12 (the pipeline answers) and a Langfuse project (below).
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
  - `langfuse FAILED error=ConnectError` or `error=ReadTimeout`: the host or the network is
    wrong. `error=LangfuseRejected`: Langfuse answered but refused the events: send this line,
    it is the first real check of the payload format (the path, event and field names were
    written from the documentation and could not be tested offline).
  - `langfuse FAILED error=Timeout`: nothing was sent within 20 s.
- **Check in the Langfuse web page** (Project, Tracing):
  1. One new trace appears, with one observation per stage (names such as `triage`, `retrieve`,
     `rerank`, `generate`) and, for the model calls, a model name and token counts.
  2. Open the trace and each observation. The input, output and metadata must hold only: stage
     names, timings, decider, model, key fingerprint, token counts, costs, flags, prompt hashes,
     the corpus version, and error type names.
  3. **There must be no question text, no answer text, no chunk text, no history, no username or
     email anywhere**: look at Input, Output, Metadata, Tags, and the trace's User and Session
     fields (those hold long hex hashes, not names). Search the trace for a word of your
     question: it must not be found.
- **Send back:** `SENT` (or the failure's error name), and for the Langfuse page only: the
  number of observations, whether Input/Output are empty, and "no question or answer text
  found" yes or no. Never a screenshot that shows the keys page.
- **Clear:** the variables in this window. The Langfuse keys stay in your password manager;
  revoke them on the API Keys page if they ever leak.
- **Real calls:** the same ~4 model calls as one question in Run 12, plus 1 call to Langfuse (a
  batch of events). Free tier: the Hobby plan has a monthly event cap that one trace does not
  approach.
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
  You should see the trace gone in Langfuse. If it is still there after five minutes: open the
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
- Phase 13 and 16 (evaluation, baseline): the evaluation runs (they use real provider keys and
  need your approval first).
- Phase 14 (load tests): the load run, with the free-tier limits it may hit.
- Phase 15 (deployment): the Render deploy steps and the proxy-header test.
