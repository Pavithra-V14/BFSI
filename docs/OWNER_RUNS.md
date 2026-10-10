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
  `allow-list entries: approved N, pending M` (about 70 to 110 in all, most approved by
  themselves), `failed pages waiting in the review queue: 0`.
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
      done: the pipeline answered

  `authenticate` is SKIPPED because the smoke run has no rate limiter; `rewrite` is SKIPPED
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
      done: the pipeline did not answer

  Exit code **1** (a refusal is "did not answer": that is the expected result here).
- **The review row:** a no-reliable-context refusal is queued for review, and the `persist OK`
  line is where that row is written. The smoke script writes it to its throw-away database,
  which is deleted when the script ends, so the row itself cannot be seen in your real
  database. The only sign you can check is `persist OK` together with `reason=no_context`. If
  you want the row visible (a `review rows: N` line, counts only), ask for it as a small change
  to the script in a later round: I have not added it.
- **If it differs:**
  - `RESULT ANSWER` with `citations=1` or more: a customer was answered. The script does not
    print which document the citation is from, so stop, keep the output, and tell your advisor:
    it is either another sample that mentions the same folio (unlikely) or a real isolation
    failure. Do not go on.
  - `reason=` something else (`out_of_domain`, `unclear`): the question was judged before
    retrieval; send the line.
  - `RESULT UNAVAILABLE`: a provider or store failed; run again.

**Step 3. Ask the same as staff: it must answer, with a citation.**

    uv run python scripts/pipeline_smoke.py --run --role staff --question "Which folio is pending nomination opt-in or opt-out?"

- **You should see:** every stage line as in the main Run 12 list (`triage` through `respond`),
  then

      RESULT               ANSWER    reason=None citations=1 warnings=0 degraded=None cached=False confidence=None tokens=NNN seconds=NN.N
      done: the pipeline answered

  `citations` is 1 or more. Exit code 0. (The answer text is not printed.)
- **If it differs:** `REFUSAL reason=no_context` as staff means the staff copy is not being
  found: run `status --doc-id ecas-staff-only` (it must say `active 21`) and send both outputs.
  `warnings=` above 0 or `confidence=low`: the answer was labelled; send the numbers.

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
  `RESULT REFUSAL reason=no_context citations=0 ...`, `done: the pipeline did not answer`, exit
  code 1.
- **If it differs:** an `ANSWER` for staff after the delete means a deleted chunk is still
  retrieved: stop and send the output (this would be a deletion or filter fault).

**Step 6. Put the customer copy back.**

    uv run python -m app.ingest ingest spec/samples/company/finserv_mutual_fund_ecas_and_servicing.pdf `
      --doc-id finserv-mf-ecas --doc-type policy --access-role customer --product-line finserv

- **You should see:** `finserv-mf-ecas: new 21, changed 0, unchanged 0, removed 0, version 1`,
  exit code 0 (quick: the answers are cached). Then `status --doc-id finserv-mf-ecas` shows
  `active 21`.

- **Send back:** for each smoke command only its role, the `RESULT` line, and the exit code;
  for each ingest, delete and status the counts lines. Never the question's answer text.
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

---

# C. Added by later phases

Rule (also in `CLAUDE.md` and the build-phase skill): every phase that has a real-service check
appends its runs here, in the same shape as above (number, name, what it verifies, before,
needs, commands, what you should see, what to do if a line differs, what to send back, how to
clear, approximate calls and limits, safe to repeat), **before the phase can be called
closed.** Also add the run to the table at the top.

Known candidates, not yet written (the phase that creates the code writes the run):

- Phase 9 (cache hardening): a real-Upstash re-check of the staleness paths.
- Phase 10 (chat API): a local end-to-end chat against real services.
- Phase 11 (Streamlit): a look at the screens, with the API running.
- Phase 12 (observability): the Langfuse retention and dashboard check.
- Phase 13 and 16 (evaluation, baseline): the evaluation runs (they use real provider keys and
  need your approval first).
- Phase 14 (load tests): the load run, with the free-tier limits it may hit.
- Phase 15 (deployment): the Render deploy steps and the proxy-header test.
