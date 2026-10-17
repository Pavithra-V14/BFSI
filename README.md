# BFSI RAG Assistant

A read-only question-answering assistant over a company's banking, financial
services and insurance (BFSI) policy and regulatory documents. Every answer is
grounded in the documents and cites clauses. Customers, staff and admins see
different documents.

Current phase: Phase 16 (baseline, targets, pre-launch gate) built, awaiting the owner's runs (35 to 39) and approval; it is not closed until the runs and the decisions they need have happened. Phases 1 to 15 are closed in code; Runs 19 to 39 are pending with the owner

## Where things are
- `spec/` : the full specification (`build.md`), two diagrams and three
  sample company PDFs. This is the source of truth.
- `CLAUDE.md` : the rules every Claude Code session reads.
- `docs/OWNER_RUNS.md` : the checklist of every run you do yourself with real keys and real
  services (what is done, what is pending, in order, with the commands and what you should see).
- `.claude/` : Claude Code settings, hook scripts and any approved skills or
  agents.
- `app/` : application code. Phase 1 holds the isolation guard and the migration runner.
  Phase 2 adds the gateway in `app/gateway/`: every model, embedding and rerank call.
  Phase 3 adds the data layer: `app/vector/` (the Qdrant `documents` collection),
  `app/cache/` (the Upstash cache primitives) and `app/store_errors.py` ("temporarily unavailable").
  Phase 4 adds accounts and the API: `app/accounts/` (passwords, login tokens, users, throttling,
  budgets, audit rows, the owner's `admin_cli`) and `app/api/` (the FastAPI app and its security layer).
  Phase 5 adds ingestion in `app/ingest/` (admin machine only: parse, chunk, index, delete, entity
  allow-list, review) and the shared sparse tokenizer `app/vector/sparse.py`.
  Phase 6 adds the guardrails in `app/guardrails/` (query limits, greetings, injection checks, PII
  masking, the context and output guards, plain-text rendering, the disclaimer) and the versioned
  prompts in `prompts/`. The pipeline that calls them is built in Phase 8.
- `scripts/` : helper scripts you run by hand, such as the gateway smoke test and the Qdrant check
  (they use real services, so they do nothing unless you add `--run`).
- `originals/` : created by ingestion; your original PDFs (git-ignored). It is the ONLY copy the
  project keeps: back it up yourself. `originals/.cache/` holds saved model answers and may be
  deleted at any time.
- `tests/` : automated tests. They only use SQLite and local resources.
- `migrations/` : numbered SQL files for the database (Phase 3 holds the full schema).
- `.github/workflows/ci.yml` : the checks that run on GitHub.
- `requirements*.txt` : the five dependency files (the API, the frontend, ingestion,
  evaluation, development). `.env.example` : the settings template.
- `frontend/` : the Streamlit frontend, a separate service that talks to the API over HTTP only.
- More folders are added phase by phase.

## How to work with Claude Code in this project
1. Open this folder in VS Code.
2. Start Claude Code. It reads `CLAUDE.md` automatically.
3. Say "begin Phase N" (or run `/build-phase N`) when you are ready.
4. Claude Code follows the build order in `spec/build.md` section 21.
5. It stops for your approval after each phase. It never starts the next
   phase on its own.

Changes to `spec/` need the owner's approval. Items tagged [P] (proposed) and
[T] (to be decided) in the spec are the ones to confirm with the owner.

## Local setup
1. Create the environment once: `uv venv --python 3.12`
2. Install runtime and development packages:
   `uv pip install -r requirements.txt -r requirements-dev.txt`
3. Run the tests: `uv run pytest`
4. Check the code: `uv run ruff check .` and `uv run ruff format --check .`

The database is a cloud Postgres (Neon or Supabase). Nothing is installed on
your machine, and local tests use SQLite.

Ingestion, entity extraction, evaluation and load tests run on the admin
machine only. The hosted service answers queries only.

## Secrets
- Copy `.env.example` to `.env` and fill in your own keys. `.env.example`
  is created in Phase 1 and contains variable names and comments only.
- Never commit `.env`. Never paste a key into a chat, a ticket or a log.
- Evaluation keys stay on the admin machine and are never put on the host.

## First run on the real services (owner only, in your own PowerShell)
Claude never runs these. Settings are read from the shell, never from a file, so each
`$env:NAME = ...` below lasts only for that terminal window. Use one window for all steps.
Keep real values in a password manager; never paste one into a chat, a file or a ticket.

**1. Make the two keys.** They must be different, random and at least 32 characters. These
commands put each key straight into the shell without printing it:

    $env:JWT_SECRET_KEY = (uv run python -c "import secrets; print(secrets.token_urlsafe(32))")
    $env:AUDIT_HASH_KEY = (uv run python -c "import secrets; print(secrets.token_urlsafe(32))")

Copy them into your password manager before closing the window (type `$env:JWT_SECRET_KEY` to
show one on your own screen). Render later needs the same two values. If you change
`AUDIT_HASH_KEY`, old audit rows can no longer be matched to usernames, so keep it.
Needs: nothing else.

**2. Apply migration 0004** (adds `users.is_active`). Use the database's direct (non-pooled) URL
with `sslmode=require`:

    $env:DATABASE_URL = "<direct URL from Neon, ends with ?sslmode=require>"
    uv run python -m app.db.migrate --dry-run   # lists the pending files, changes nothing
    uv run python -m app.db.migrate             # applies them
    uv run python -m app.db.migrate --dry-run   # again: it should list nothing pending

Needs: `DATABASE_URL` only. It is safe to run twice; applied files are skipped.

**3. Create the first admin.** It asks for the password with a hidden prompt:

    uv run python -m app.accounts.admin_cli create-admin --username NAME --email ADDRESS

Needs: `DATABASE_URL` only (same window as step 2). Only this command can make an admin; the
API cannot. `set-active --username NAME --active no` turns an account off.

**4. Run the API locally:**

    $env:UPSTASH_REDIS_URL = "<rediss:// URL from Upstash>"
    uv run uvicorn app.api.main:create_app_from_env --factory

Needs: `DATABASE_URL`, `UPSTASH_REDIS_URL`, `JWT_SECRET_KEY`, `AUDIT_HASH_KEY`. It refuses to
start if any is missing, short or a placeholder. Optional, with defaults: `JWT_EXPIRE_MINUTES`,
`TOKEN_BUDGET_CUSTOMER`, `TOKEN_BUDGET_STAFF`, `PASSWORD_HASH_CONCURRENCY` (2),
`CORS_ALLOWED_ORIGIN` (none). It serves on http://127.0.0.1:8000; check `/live`. Stop with Ctrl+C.
Do not trust `X-Forwarded-For` or deploy behind a proxy until Phase 15 sets the proxy options.

## Ingestion on the admin machine (Phase 5)
Ingestion turns a PDF into searchable chunks. It runs only on your machine, never on Render, and
you start it yourself in your own terminal. Claude never runs it on real services and never reads
your `.env`.

**Before the first run (once):**
1. Install poppler (turns PDF pages into pictures) and tesseract (reads text from scanned images).
   Both must be on your `PATH`. Check in a new terminal: `pdftoppm -h` and `tesseract --version`
   each print something instead of "not recognised".
2. Install the ingestion packages: `uv pip install -r requirements-ingest.txt`. This also downloads the
   spaCy language model `en_core_web_lg` (about 400 MB), so no separate model step is needed.
3. Create the Qdrant collection if you have not: `uv run python -m app.vector.documents --ensure`.
4. Check your real Qdrant (it makes a throw-away collection named `check_...`, never touches
   `documents`, and deletes it again): `uv run python scripts/qdrant_check.py --run`.
   It needs `QDRANT_URL` and `QDRANT_API_KEY` in your shell. Every line should say PASS.

**Rules that always hold**
- **Only use the sample documents for now.** Clause text, table text and images are sent to model
  providers (images to Gemini). A `customer-specific` document is **refused** with a clear message:
  even without captions its text would still be embedded by a model provider. This stays so until a
  paid or zero-retention plan exists (decided again in Phase 16).
- `--doc-id` is a short name you choose and keep (letters, digits, hyphens). It is never taken from
  the file name. Ingesting a changed PDF with the same `--doc-id` updates only what changed.
- Every command is safe to run again. A run that stops (a limit, the network, Ctrl+C) never leaves
  a half-activated document: the old version stays active until a run has every page.
- After any command, type `$LASTEXITCODE`: `0` done; `1` an error, or "ingest incomplete" (a cache
  step failed: run the same command again); `2` a page failed, so the document was not activated;
  `130` you pressed Ctrl+C.
- Output shows counts and names only: never a key, an address or document text. (The dry run also
  lists the header and footer lines it removed, shortened, so you can see no real text went.)

### The real-run procedure
Work in one PowerShell window, in the project folder, in this order. Each step says what you should
see and what to do if you do not.

**(a) The variables each command needs.** Set them in the window (`$env:NAME = "value"`, gone when
you close it), or add `--load-env` to a command to read the ones you have not set from the project's
`.env` file. `--load-env` is off unless you write it; it reads only the names this tool needs, a
variable already set in the window wins, it prints the names it loaded and never a value, and it
refuses to run on Render.

| Command | Needs |
|---|---|
| `status`, `review entities` | `DATABASE_URL`, `QDRANT_URL` (and `QDRANT_API_KEY` if your Qdrant uses one) |
| `review chunks`, `delete` | the above plus `UPSTASH_REDIS_URL` |
| `ingest` | all of the above plus `GEMINI_EMBEDDING_KEY_A` (and `_B` if you have it) and `GEMINI_API_KEYS` (image captions); `GROQ_API_KEYS`, `OPENROUTER_API_KEYS` and `JEV_OPENROUTER_API_KEY` are optional (labels and summaries try them first, then Gemini) |
| optional | `COMPANY_NAMES`, `COMPANY_DOMAINS` (your real company name and domains; the sample company is the default) |

Try it: `uv run python -m app.ingest status --doc-id insurance-flexi-property --load-env`

- You should see: `--load-env: loaded N variable(s): ...` (names only), then
  `chunks: active 0, held back 0, rejected 0, inactive 0 (stored 0)` and
  `allow-list entries: none`. Exit code 0.
- If it says `error: these settings are not set: ...` the names listed are missing: set them or fix
  the line in `.env`. If it says `a store could not be reached`, check the URL and your network, then
  run it again.

**(b) One small document first** (the insurance sample: 18 chunks, 1 picture, 7 tables). Count, then
ingest:

    uv run python -m app.ingest ingest spec/samples/company/insurance_flexi_commercial_property.pdf `
      --doc-id insurance-flexi-property --doc-type policy --access-role customer `
      --product-line insurance --title "Flexi Commercial Property Policy" --dry-run

- You should see about: `pages parsed: 3 of 3`, `running headers and footers removed: 12` followed by
  12 `removed, page N: ...` lines (the document code, "Page N of 3", the company line and the sample
  notice, on each page), `chunks: 18 (text 11, table 7); images: 1`, `embedding calls 2`,
  `image caption calls: 1; table summary calls: 7`, `clause label calls: 19`,
  `entity questions: 11 of 55 candidates`, and `no service was called (dry run)`. Nothing is called.
- If a `removed` line is real text, stop and tell Claude. If `pages parsed` is lower than the page
  count, a page failed to parse (see (h)). A `warning: page N: a table lost K word(s)` means a table's
  cells are not complete: tell Claude.

Then the same command without `--dry-run` (add `--load-env` if you use it). It takes several minutes
(about a minute to read the PDF, then the model calls).

- You should see a `note:` line, then `insurance-flexi-property: new 19, changed 0, unchanged 0,
  removed 0, version 1` (18 chunks and 1 picture caption). Exit code 0.
- `ingest incomplete` (exit 1): the chunks are written but a cache step failed. Run the same command
  again: it says `finished the cache steps an earlier run had left undone`.
- `the model providers could not answer` (exit 1): a free-tier limit or a key problem. Wait a few
  minutes and run it again.
- `NOT activated ... page N failed` (exit 2): see (h).

**(c) The same ingest again: it must be a true no-op.** Run exactly the same command.

- You should see `insurance-flexi-property: new 0, changed 0, unchanged 19, removed 0, version 1` and
  `nothing changed: no write, no version bump, no audit row`. It makes no model call.
- If `new` or `changed` is not 0, the document is not being recognised as the same: stop and tell
  Claude. Do not go on to (d).

**(d) Status.** `uv run python -m app.ingest status --doc-id insurance-flexi-property`

- You should see: `chunks: active 19, held back 0, rejected 0, inactive 0 (stored 19)`,
  `document version: 1`, `unfinished cache step (pending-finish marker): no`,
  `allow-list entries: approved N, pending M` (about 70 to 110 in all, most approved by themselves),
  and `failed pages waiting in the review queue: 0`.
- A marker of `YES` means an earlier run did not finish its cache steps: run the `ingest` command
  again, or the `review chunks` / `delete` you were doing, and it clears.

**(e) Review.** `uv run python -m app.ingest review chunks --doc-id insurance-flexi-property` should
say `no chunk is held back for this document` (the samples contain no instruction-like text; if one
is held back it shows the page, the clause and why: read it, then `a` approve, `r` reject, `s` skip,
`q` quit). Then `uv run python -m app.ingest review entities --doc-id insurance-flexi-property`
shows the names and codes the program was not sure about, each with the sentence it appears in. `a`
approves the group (full name, "Mr. Sharma", "R. Sharma"); the surname alone needs `t`; `r` rejects;
`s` skips; `A` approves all the rest; `q` quits. Run `status` again: the `pending` count is lower.
Both commands stop with a clear message if an ingest is running on the same document; run them again when it ends.

**(f) The delete test.** **A delete records the id as withdrawn for good** (so that a restore can
never bring it back), so this drill is done on a **throw-away id** and never on a real one. Ingest
the same sample under `rehearsal-insurance-1` (the command of (b) with `--doc-id
rehearsal-insurance-1`; quick, the answers are cached), then run
`uv run python -m app.ingest delete --doc-id rehearsal-insurance-1`, which asks you to type the id.
You should see `rehearsal-insurance-1: removed 19 chunk(s), N allow-list entr(ies); 0 chat
message(s) flagged 'source removed'`, then `recorded as withdrawn (reason: other); 1 original
PDF(s) moved`. Then `status --doc-id rehearsal-insurance-1`: `stored 0`,
`allow-list entries: none`, marker `no`. The next drill uses `rehearsal-insurance-2`.

**(g) The other two documents** (same pattern):

    uv run python -m app.ingest ingest spec/samples/company/banking_rtl_standard_facility_agreement.pdf `
      --doc-id banking-rtl-facility --doc-type policy --access-role customer --product-line banking
    uv run python -m app.ingest ingest spec/samples/company/finserv_mutual_fund_ecas_and_servicing.pdf `
      --doc-id finserv-mf-ecas --doc-type policy --access-role customer --product-line finserv

You should see `new 22 ...` for the banking one and `new 21 ...` for the finserv one. Run (c) and (d)
for each. **Approximate real calls for all three documents**, from the dry run: 6 embedding calls
(16 texts each), 3 image captions, 22 table summaries, 62 clause labels and 29 entity questions.
**The free tiers can slow or pause the run** (the Gemini limits are unpublished; Groq allows about 30
requests a minute). The program waits for room, and when a provider still says no it stops with a
clear message. That is safe: run the same command again, and the calls that already worked are not
repeated.

**(h) A failed page and its retry.** Use the banking document, which you have just ingested:

1. Use a **throw-away id**, not the real one (a delete records an id as withdrawn for good):
   every command below uses `--doc-id rehearsal-banking-1` instead of `banking-rtl-facility`
   (next drill: `rehearsal-banking-2`). There is nothing to delete first.
2. Remove the saved answers so the picture is asked for again: `Remove-Item -Recurse -Force originals\.cache`
3. Break the Gemini key for this window only (the caption has no other provider):
   `$env:GEMINI_API_KEYS = "not-a-real-key"`
4. Run the banking ingest command with `--doc-id rehearsal-banking-1`. You should see
   `document 'rehearsal-banking-1' was NOT
   activated; the old version (if any) stays active`, one or more lines `page N failed (<an error name>)` (at least page 2, which holds
   the picture; table pages too if no other provider key is set), and `each failed page is in the review
   queue; run the same command again to retry`. Exit code 2.
5. `status` shows `stored 0` and `failed pages waiting in the review queue:` as many as there were
   `page N failed` lines.
6. Put the real key back (close the window and open a new one, or set the variable again; use
   `--load-env`), run the same ingest command again. You should see `new 22 ...`, exit code 0, and
   `status` shows `failed pages waiting in the review queue: 0`.

If a step differs from what is written here, stop, copy the lines the program printed (they hold no
secrets) and tell Claude.

### Known limitations of ingestion (recorded in Phase 5; the number is the phase that handles it)
- **Phase 16 (baseline run).** Table identity depends on the table's order under its clause. A paragraph that
  starts with a number like "1.5 times ..." is taken as a clause. A table with only a border and a header
  line can be read as two rows. A clause `~N` suffix can differ between parts of one clause. A clause
  label of "general" given when no decider answered is not retried unless the chunk changes. A page that
  parses to nothing counts as a failed page, so a scanned PDF whose OCR finds no text, or a deliberately
  blank page, blocks activation: scanned-PDF behaviour is decided here. Customer-specific documents
  stay refused until a paid or zero-retention plan exists.
- **Phase 6 (guardrails and masking).** Organisations, places and laws that the NER model finds are
  approved automatically and can include harmless words (for example "Facility"); masking must be
  measured against them. `normalise_entity` trims spaces, quotes, commas, `; : ( ) [ ]` from both
  ends of a stored term and keeps a full stop (for example "Pvt. Ltd." is stored as "pvt. ltd."):
  masking uses the same function, so the two always agree.
- **Phase 10 (chat, session and admin API).** Allow-list rows are saved before the chunks are
  written, so a run that fails in between leaves rows for text that was never indexed (harmless);
  the admin entity endpoints should clean them up. The entity review does not group titled people
  ("Dr. Rao" and "Dr. Anita Rao" show as two groups), and a group of one single word answered `a`
  approves nothing and says so only in the count. The audit "who" is `cli:<operating-system login>`;
  the admin ids of Phase 10 replace it, and `approved_by` stays empty until then.
- **Phase 12 (observability).** A delete retried after a database failure can record a chunk count of 0
  or no audit row.
- **Phase 15 (deployment and runbooks).** Qdrant writes go in batches of 64 and are not atomic; a
  failure half way leaves a partly updated document, and running the same command again finishes it.
  A delete leaves the saved answers, the original PDFs and open failed-page rows; they are removed
  by hand (the runbook says how).

## Guardrails and prompts (Phase 6)
These are the local checks around every question and answer. They run on Render, call no model and
need no key. They are functions the pipeline (Phase 8) will call in the spec's order:

1. **Local scan** (`app/guardrails/scan.py`): at most 100 tokens (never cut silently); a
   whole-message greeting or farewell gets a fixed reply (this is checked before the minimum, so a
   first message of "hi" is answered, not refused); a first message of at least 10 characters (a
   follow-up only has to be non-empty); the injection regex is recorded; names, phones, emails and
   identifiers (PAN, Aadhaar, cards, policy and account numbers) are replaced by `[PERSON]`,
   `[PHONE]`, `[EMAIL]` and `[ID]` before any model sees the question. Places are not masked.
   Masking looks at a normal form of the text (full-width and Devanagari digits, odd spaces,
   zero-width characters, dot or slash separators, numbers written in groups of digits) and replaces
   the original characters. Names typed in capitals or in lowercase get a second look with the
   name-like words capitalised; the approved-name list and the product-word protections still apply.
   The result has two kinds of copy, and the code keeps them apart: `ModelCopy` (pass 1, for models
   only; printing it raises an error) and `StoredQuery` (pass 2, `mask_for_storage`, made from the
   raw question with only the surviving documents' names: the only copy that may be stored, logged
   or cached).
2. **Injection decision**: a strong regex hit or a Jev score of 0.85 or more blocks; a weak hit or a
   score from 0.50 asks the user to reword. Ordinary questions such as "show the instructions for
   filing a claim" or "how do I remove the restrictions on my demat account" are not attacks; asking
   for the assistant's own prompt ("your instructions", "system prompt") is. These numbers are
   starting values for the Phase 16 baseline.
3. **Context guard**: drops chunks below a relevance of 0.30 and chunks that read like instructions
   to the model; if nothing is left the answer is a fixed refusal. What is left goes into the prompt
   between clear delimiters that a chunk cannot forge.
4. **Output guard**: the answer must be JSON with citations; every number, percentage, date and
   amount must appear in the cited chunks; every citation must be a document the model was given; the
   words of the answer must be found in the cited text (0.6 to start). If a check fails the answer is
   still returned, with a warning and the unchecked numbers marked, and it is not cached. There is
   ONE flag, `grounded`: it is true only when the answer parsed, cites at least one real document,
   every citation is real, every figure is backed and the words are covered. Any failed check
   clears it.
5. **Plain text**: model output is shown as plain text. HTML and markdown link syntax are removed; a
   web address stays only when its domain is a regulator's or the company's (`COMPANY_DOMAINS`).
   The domain is the part before the first `/`, `?`, `#` or `\`, so `evil.com/x?next=a@irdai.gov.in`
   does not pass as `irdai.gov.in`. Invisible and direction-changing characters are removed, also
   when they were written as HTML entities.
6. **Disclaimer**: added only for advice-like questions or answers with a financial figure.

**Prompts** are files in `prompts/` named `name.v1.md`, each with the same sections (role, task,
constraints, output format, a good and a bad example, a refusal rule). Changing a prompt means a new
file or a new version; its SHA-256 hash is recorded with every answer. The image-caption and
table-summary prompts used by ingestion live there too, word for word as before.

**Known limitations (recorded in Phase 6)**
- **Phase 8 (pipeline).**
  - Check a Jev injection score before using it: a NaN or out-of-range value falls through to "pass".
  - Keep `MaskResult.spans` out of traces and logs (they show where in the text a name or number was
    and how long it was).
  - Fail closed: if the masker raises, refuse the question; never send the unmasked text on.
  - Pass 2 is made from the RAW question kept in memory, after retrieval, with the ids of the
    surviving documents (`mask_for_storage`); only that `StoredQuery` is stored, logged or cached,
    and the cache also needs: no refusal, no low-confidence flag, an unchanged corpus version, and
    `grounded` true with no warning.
  - Load the masker in the background at start-up. Until it is ready, never skip masking: answer
    "starting up, please retry".
- **Phase 15 (deployment).** Deploy `prompts/` next to `app/` (the prompt load stops if the folder
  is missing). Pin the `en_core_web_sm` wheel by hash (its direct link has none today). Measure the
  total memory of the whole service against the 512 MB host (only the masker was measured: about 140
  MB with the model loaded). Editing only the comments of a prompt file still changes its version.
- **Phase 16 (baseline run).** Recall and precision items, all measured then:
  - First masking task: add a short list of common Indian first names and surnames as a third
    check, then re-run the 17 sample capitalised/lowercase names and the 34 ordinary sentences
    (`CASED_NAMES` and `ORDINARY_CASED_TEXT` in `tests/test_guardrails_round2.py`).
  - Masking: the small spaCy model (`en_core_web_sm`) has limited recall for Indian names. A name in
    capitals or lowercase is found only when the model recognises it in a capitalised copy and it has
    two words or more (for example "ask for priya nair" and "claim of deepak verma" were missed, and
    "suresh kumar iyer" lost its first word); a person-like product name ("Jeevan Anand") is masked
    unless an approved document names it. Labels beyond the list (UTR, RRN, transaction id, vehicle
    registration, IMEI), numbers written in words, single-spaced digit groups, numbers split by a
    line break, "name at example dot com", a PAN written with spaces, a labelled value longer than
    25 characters, a fourth name word, and the word "Max" as a product tier are not handled or are
    handled only in part.
  - Injection: the regex does not undo zero-width characters, look-alike letters, leet-speak, base64
    or rot13; the context guard does not scan the header fields of a chunk or look-alike brackets.
  - Output guard: number words, signs, units glued to a number, a day and month without a year, and
    `$` or USD amounts are not checked; a page or clause number counts as evidence; groundedness
    ignores "not" and "no" (so "war risks are covered" scores as grounded against "war risks are not
    covered"); a bare `evil.com/login` and a `mailto:` link in an answer are kept.
  - Query token count: if the tokenizer file is missing, the fallback (length divided by 4) counts
    non-Latin text too low; the flag `approximate_tokens` only reports it.
  - Prompts: no check for a repeated header key; a line starting with "## " inside an example starts
    a new section (the load then refuses the file, which is safe).
  - Isolation: the import test does not see relative imports, `importlib` or `__import__`; nothing
    yet catches Phase 8 building the masker at import time; two requests masking at the same moment
    are not tested.
  - All thresholds above are starting values.
- **Measured on the admin machine** (Intel Core i5-6300U, 2 cores, 7.9 GB, Windows 10, Python 3.12):
  the first masking call loads the model and takes about 40 seconds (46.6 s with imports); after that
  the whole local scan, including the second look at capitals and lowercase names, took a mean of
  19 ms, a median of 18 ms and a 95th percentile of 30 ms over 80 questions (the slowest took 83 ms).
  The unit tests check that every step is timed and the steps add up; they
  no longer assert a number of milliseconds, because that fails on a busy machine for no reason in
  the code.

## Memory and sessions (Phase 7)
Short-term memory only (no memory across sessions). The code is in `app/memory/`; these are
functions the pipeline (Phase 8) and the chat API (Phase 10) will call. Nothing here is a web route.

- **Sessions and messages** (`store.py`): every function takes the user id, and a session that
  belongs to someone else gives the same "not found" as one that does not exist; a closed session
  gives it too when you try to write to it. Message text is accepted only as `StoredQuery` (a user
  message) or `StoredAnswer` (an assistant message), and a summary only as `StoredSummary`. Those
  three types can only be made by `mask_for_storage` and by the summary function: calling the class,
  copying it or building a look-alike is refused (tests use `tests/stored.py`, which nothing in the
  application may import). A fixed reply (a greeting, the unclear-turn ending, a refusal) is stored
  with `add_fixed_reply`, always ungrounded. A question that tripped the injection scan or was
  refused is stored with `history_excluded` and never feeds history. Two messages never share a
  number (the database's unique constraint decides and a collision is retried). Every read is inside
  a transaction block and a time given to the package must carry a time zone.
- **History** (`buffer.py`, `summary.py`, `history.py`): the rewrite step gets a running summary
  (at most 150 tokens) plus the newest turns word for word within `MEMORY_BUFFER_TOKENS` (default
  400, a starting value). Only grounded answers whose source is still there feed it; each whole
  history line is clipped to about 80 tokens. When turns no longer fit, one decide-tier call folds
  the oldest of them (at most 1,500 tokens, a starting value) into the summary; the rest waits for
  the next turn. The reply is made plain text, masked, scanned for instruction-like text and clipped;
  on a hit the old summary stays and an alert event is emitted. Any error in the fold, of any kind,
  is only a failed fold: the turn goes on. A summary is not saved if a folded message was flagged
  "source removed" while the model worked. No database transaction is open while the model is
  called. Everything placed in the rewrite prompt has its angle brackets replaced. The generator
  never sees any of this.
- **Facts and unclear turns** (`facts.py`, `unclear.py`): `session_facts` holds the product line,
  topic and approved names (each with the documents that approve it) found by plain matching (no
  model call), checked when written and again when read for a prompt, and the count of unclear turns
  in a row. That count is never put in a prompt. Two in a row end with a fixed message and one
  `two_unclear_turns` row in the review queue.
- **Retention** (`retention.py`): chats not used for 90 days (counted from the last activity) are
  deleted, with one audit row of counts (none when nothing was deleted; a chat resumed while the
  sweep runs is kept). To see what would go, in your own terminal:
  `uv run python -m app.memory.retention --dry-run` (it needs `DATABASE_URL` in the shell and only
  counts). Without `--dry-run` it deletes. Scheduling it is a deployment step (Phase 15).
- **Deleting a document** (`documents.py`, called by the Phase 5 delete): a summary that rests on
  the deleted document is cleared, and the document is removed from the names kept in session
  facts (a name another document also approves stays); one audit row of counts.
- **A role change** (`roles.py`, called by `set_role` in the same transaction): the user's
  sessions are closed and their summaries and facts cleared; the messages stay in the transcript.

**Known limitations (recorded in Phase 7)**
- **Phase 8 (pipeline).** Use `facts_for_prompt` / `render_facts` for anything that goes into a
  prompt; use `prepare_history` before the rewrite; store messages only through the masked types;
  decide "unclear" from the Jev vague flag and call `record_turn`; store a question that tripped the
  injection scan or was refused with `history_excluded=True`; store greetings, the unclear ending and
  refusals with `add_fixed_reply`; connect the alert events (`summary_rejected_injection`,
  `summary_fold_failed`) to the alert path. **One turn at a time per session:** two requests in one
  session (two browser tabs) get different message numbers but their rows can interleave, so the
  pipeline must refuse or queue a second question while one is being answered. When the summary call
  fails, the turns that no longer fit are left out of that turn's history and tried again on the
  next turn.
- **Phase 10 (chat API).** The routes call these functions with the signed-in user's id; a wrong
  session id must give the same answer as a missing one at the HTTP level too (404 both ways). Same
  rule as Phase 8: one turn at a time per session. **Deleting a user account** must first run the
  review-text clearing (`app.db.chats.clear_review_details_for_session`) for each of the user's
  sessions, because the database cascade removes the messages but cannot erase the text copied into
  review items.
- **Phase 12 (observability).** The two summary events are plain callbacks until the alert path
  exists. The retention sweep is one transaction; a very large backlog should be swept in smaller
  batches.
- **Phase 15 (deployment).** Schedule the retention command (daily is enough).
- **Phase 16 (baseline run).** Sweep the buffer size (200, 400, 800, 1,200) on the multi-turn set;
  the 150, 80 and 1,500 token limits and the fixed "unclear" message wording are starting values.
  `product_line` and `topic` in session facts are not cleared when a document is deleted (only the
  names are); the stored text of "source removed" answers and the approved names inside stored
  questions stay in the transcript, and the pipeline must not show or reuse them.
- **Postgres.** `tests/test_postgres_memory.py` covers every new query but was written without a
  Postgres on this machine; its first run is the first CI run.

## Retrieval and the query pipeline (Phase 8)
The code is in `app/pipeline/`. One call, `Pipeline(deps).answer(request, db)`, takes a question
from a signed-in user and runs the 17 stages of the workflow diagram as a LangGraph graph. It has no
web route of its own (the chat API is Phase 10).

- **The stages** (`graph.py`): rate limit and budget; local scan (limits, greeting, injection regex,
  masking); Jev triage; injection decision; rewrite (only when needed); domain gate; exact cache
  lookup; embed; hybrid retrieval; rerank; context guard; confidence; generate; output guard; cache
  write; persist; respond. Early exits (a greeting, a block, an out-of-domain question, a cache hit,
  no reliable context) skip the rest and are stored.
- **Retrieval** (`retrieval.py`): one Qdrant call runs the dense and the sparse search and fuses them
  (RRF, k = 60, 25 candidates). The user's role limits the documents (customer sees `customer`; staff
  `customer` and `staff`; admin all three) and only active chunks are searched, on both branches. A
  confident product line or clause type narrows the search; if that finds nothing, or nothing is above
  the relevance floor after the rerank, it is repeated once without (same role and active filters, and
  only if the time left allows it). The values offered to the triage are those of documents the role
  may see. Rerank keeps the top 5.
- **Answers** (`answer.py`): the generator gets the instructions, the labelled chunks and the
  question, never the chat history. A failed number or citation check still returns the answer, with a
  warning and the unchecked numbers marked. Low confidence (best score under 0.40) answers with a
  statement that it may be wrong and goes to the review queue; no usable chunk gives the fixed refusal.
- **Time**: 30 seconds for the whole request. Each step gets the smaller of its own limit and the time
  left, and a step is skipped when less than its minimum is left (the minimums are `[P]` in spec 4.5;
  the answer model needs 6 s). The database and counter calls of the first two steps have their own
  0.4 s slices: a slow rate limiter or budget counter lets the request through with an alert, an
  allow-list that cannot be loaded masks with an empty one (masking is never skipped), and a slow
  Upstash or Qdrant look-up in triage is dropped after its slice.
- **A follow-up that needed history** is never refused as out of domain by the first triage, even if
  its rewrite failed or timed out; then it is answered from its bare text as in-domain, with low
  confidence (it says it may be incorrect, goes to the review queue, is never cached) and with the
  soft filters off. A vague first message has no history and is still judged by the gate.
- **Tokens**: the tokens every model call really used (including the summary fold, which has its own
  span) are charged once from the trace in a `finally`, so a timeout, an error or a cancellation still
  charges what was spent.
- **Safety** (`safety.py`, `startup.py`, `locks.py`): LangChain / LangSmith tracing is switched off in
  code and the graph is compiled with no checkpointer (the state holds the raw question); the masker
  loads in the background and until it is ready the answer is "starting up"; one turn at a time per
  session (the session stays locked until a transcript write that timed out has really ended, and the
  result says `db_busy` while it runs); a failed dependency gives "temporarily unavailable", never an
  ungrounded answer; if Postgres fails the answer is returned and the masked turn is queued in
  memory (also when the 30 s ended first). A queued turn that keeps failing while the database
  answers is set aside after 5 tries with an alert, so it cannot block the others. The text in the
  cache is exactly the text that is stored (the pass-2 masked string).
- **Result** (`state.Result`): kind (answer, reply, refusal, reword, unavailable, starting_up, busy,
  rate_limited, over_budget, session_not_found), text, citations, warnings, confidence, degraded
  notices, whether the disclaimer was added, per-stage timings, tokens and an audit dict of ids and
  hashes (no text).
- **A real-service check you can run yourself**, in your own terminal (it asks one question through
  all 17 stages and prints names, timings and counts only, never the question, the answer or a key):
  `uv run python scripts/pipeline_smoke.py --run` (add `--role staff`, `--question "..."` or
  `--with-cache`). It uses your real keys, a throw-away SQLite file for the chat tables, and never
  opens your real database. It READS your real Qdrant and never writes to it; Upstash is touched only
  with `--with-cache`. Nothing happens without `--run`. It uses the variables already in your shell;
  add `--load-env` to let it read your `.env` file itself (only the names it needs, never printed).
- **Dark mode switch** (`frontend/theme.py`, sidebar, **[P]**, Change round 1). Starts from your
  browser's theme; the page's own colours change at once and, when you pick the other theme,
  CSS rules repaint Streamlit's own widgets (Run 45 is the look at every screen).
- **Purge all data** (`app/maintenance/`, **[P]**, Change round 1). `uv run python -m app.maintenance
  purge --dry-run` shows what would be erased; without `--dry-run` it asks you to type `DELETE ALL
  DATA`. It erases the document vectors, the cached answers, every chat, the customer and staff
  accounts, the review queue, the alert rows and the metrics. It keeps the admin accounts, the audit
  log, the allow-list entities, the withdrawn-document list and the settings. The same thing is the
  button "Purge Database Data" on the Maintenance tab (admin machine only). Never on Render.
- **Langfuse sink** (`app/pipeline/langfuse_sink.py`, **[P]**). Off by default. It turns on only when
  `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` and `LANGFUSE_USER_HASH_KEY` are all
  set to real values (the last must be its own key, not `JWT_SECRET_KEY` or `AUDIT_HASH_KEY`).
  It sends the trace of every request through the Langfuse SDK (pinned in `requirements.txt`; no
  decorator, no integration, no automatic capture: our code sets every field). One observation per
  stage, typed (retriever, embedding, guardrail, generation), with the real start and end time, and
  its input, output and metadata (`app/pipeline/langfuse_content.py` says exactly what). **Text:**
  only the MASKED stored question and answer (the pass-2 copies the chat store keeps; withheld if
  they still look like personal data), the retrieved passages and the answer prompt (company
  documents and our own instructions). Never the raw question, the copy the models receive, a
  rewritten question, chat history, a summary, a username or an email; the user and session appear
  only as keyed hashes. `LANGFUSE_SEND_TEXT=false` sends no input or output at all. A Langfuse
  failure or slowness cannot touch an answer: the SDK sends in its own threads with a 3 s timeout,
  a bounded queue of 200 drops when full, and it all happens after the answer is final. Alert events: `langfuse_failed`, `langfuse_dropped`, `langfuse_disabled` (type names only,
  at most one a minute). To try it yourself: `uv run python scripts/pipeline_smoke.py --run --langfuse`
  sends one real trace and prints only `SENT`, or the exception type that stopped it.

**Known limitations (recorded in Phase 8)**
- **Phase 9 (cache hardening).** A hit is verified (chunk hashes, active, role) and an entry is written
  only under the rules of spec 10.2, but the role and permission version bumps, the sparse-index
  refresh and the other staleness scenarios are Phase 9. The semantic cache is not built.
- **Phase 10 (chat API).** The route must check the token, build the `Deps` and the per-request
  database connection, start the masker holder at start-up, and map the result kinds to answers:
  `starting_up` and `unavailable` to 503, `busy` to 409, `rate_limited` to 429 with `retry_after`,
  `over_budget` to 429, `session_not_found` to 404 (the same for a missing and a foreign session).
  The correlation id is returned internally and is not shown to users. The lock and the persist queue
  live in one process (one Render instance); several instances would need a shared lock. **Close the
  request's database connection only when `Result.db_busy` is false**: a transcript write that timed
  out keeps using it until it ends (give such a request a connection that is closed when the write
  has finished, or use a pool). A first turn that had to be queued has no session id yet, so the user
  is not told one and the next message starts another session: the route should say so, or the
  client should retry the same message.
- **Phase 12 (observability).** The Langfuse sink exists (Phase 8); Phase 12 builds dashboards,
  retention and evaluation on top and may later decide whether masked text is sent. The alert
  events are plain callbacks: `stage_failed`, `persist_queued`, `persist_queue_overflow`,
  `persist_masking_failed`, `masker_load_failed`, `masker_unavailable`, `cache_entry_discarded`,
  `cache_write_skipped`, `vector_store_rejected`, `triage_failed`, `triage_probability_invalid`,
  `rewrite_failed`, `rewrite_unreadable`, `rewrite_rejected_injection`, `rerank_failed`,
  `allowlist_unavailable`, `session_unavailable`, `request_budget_exhausted`, `graph_failed`,
  `tracing_switched_off`, `summary_rejected_injection`, `summary_fold_failed`, `budget_charge_failed`,
  and since round 2 `rate_limit_unavailable`, `budget_check_unavailable`, `persist_flush_skipped`,
  `persist_job_dead_lettered`, `soft_retry_skipped`, `soft_retry_failed`, `filter_values_unavailable`.
  A turn queued while its session was still unwritten loses its Langfuse session link, and what
  Langfuse shows for a trace (the observation types, the waterfall, the input and output) cannot be
  checked offline: your own `pipeline_smoke.py --run --langfuse` is the check (Run 13).
- **Phase 14 (testing completion).** Masking runs on the event loop thread and takes about 20 ms; load
  tests must show whether that matters. Masking and the building of the stored copy run on the event
  loop, so their time limits cannot interrupt them. A step that times out leaves its worker thread
  running until it ends (the answer is already returned; the threads never change the request, and
  the session lock waits for a transcript write). The cache look-up (1 s) covers three round trips
  (version, entry, read-back from Qdrant), so on a slow day it is skipped; its version check and its
  write are not one atomic step (the read-time verification covers the gap). Measure both under load.
- **Phase 15 (deployment).** The persist queue is lost if the host restarts. Deploy `prompts/` next to
  `app/`.
- **Phase 16 (baseline run).** Every number in spec 4.5 is a starting value: the 0.30 and 0.50 floors,
  0.40 low confidence, 0.60 out of domain, 0.80 soft filters, 0.50 history, the node timeouts and the
  3,400-token budget estimate. Also to review there: the keyword rules of the no-LLM triage, whether a
  bare follow-up should be checked for domain, the adjacent clause for neighbour context, and the
  optional small caches.

## Cache hardening and staleness (Phase 9)
The exact answer cache (`app/cache/store.py`, `app/pipeline/cache_stage.py`) is now covered by a test
for every staleness rule of spec 10.3, and four things were added or fixed:

- **The role map is part of the key.** The cache key holds a short hash of the role-to-visible-roles
  map (`app/pipeline/roles.py`). If that map ever changes, every older entry becomes unreachable at
  once (and reachable again if the map is restored).
- **One bad key is not an outage; a refusing server is.** A refusal of one command (a key of the
  wrong type) is one alert (`cache_error`): it does not switch the cache off for 30 s, and that
  subject's counter is healed so it keeps being counted. A connection problem, and the refusals that
  say the whole server will not work (read-only `cache_read_only`, out of memory
  `cache_out_of_memory`, the daily request quota `cache_quota_exceeded`, no permission
  `cache_permission_denied`) open the 30 s skip window, each with its own alert name. While the
  server is down or refusing, the rate limiter still fails open with its alert (spec 11.1): that is
  the documented rule, not a bug. A key is deleted only when the error says WRONGTYPE. An entry is
  never written unless every reverse set accepted it (an entry that cites no chunk, or a chunk with
  no content hash, is not cached), and a huge reverse set is removed in batches of 500.
- **A preflight before any change.** `ingest`, `delete`, a review approval and `refresh-sparse` first
  ask whether Upstash accepts the commands a staleness write needs, and refuse to start with `cache
  unavailable, nothing was changed` if not (an ingest asks before it pays for a model call). Run the
  same command again when Upstash is back. **Residual window:** Upstash can still fail between that
  check and the version bump; the run then says it is incomplete and keeps its marker, and until you
  run the same command again an answer that cited the document (but not a chunk that was just added)
  can be served. The marker is a local file on the admin machine.
- **The tokenizer version of the sparse index.** Every chunk records `sparse_version`. If the
  tokenizer ever changes (raise `SPARSE_VERSION` in `app/vector/sparse.py`; a test fails until you do),
  run `uv run python -m app.ingest refresh-sparse` (add `--doc-id NAME` for one document). It rewrites
  only the sparse vectors from the stored text: no PDF, no embedding call, one cache-version bump at
  the end. `ingest status` shows `sparse vectors out of date: N of M`; the smoke script and the
  pipeline's start-up check (`Pipeline.check_index()`, an alert event `sparse_version_mismatch`) warn
  with counts. A repeat `ingest` of a document also refreshes its stale unchanged chunks.
- **A real-Upstash check you can run yourself:** `uv run python scripts/cache_check.py --run`. It runs
  the real cache code against your Upstash under one unique throw-away key prefix, never touches a real
  key, never flushes, removes its own keys in a `finally`, and prints names and counts only. See
  `docs/OWNER_RUNS.md` (Run 14).
- **Decided, not built:** the semantic cache (revisit after Phase 12 shows the exact cache's hit rate:
  a semantic hit can serve a wrong answer to a near-identical question that differs in a number) and
  the two small caches (query embeddings, triage results).

**Known limitations (recorded in Phase 9)**
- **Phase 10.** The chat API must call `Pipeline.check_index()` once at start-up (it makes one Qdrant
  count call; run it in a thread) and show the alert.
- **Phase 12.** New alert events: `cache_error`, `sparse_version_mismatch`,
  `sparse_check_unavailable`. The cache hit rate (exact) and the stale-answer incident count are
  measured there; the semantic-cache decision waits for them.
- **Phase 14.** A very large corpus makes the start-up check's two count calls slower (they are exact
  counts); the refresh is sequential, document by document, and the filter on `sparse_version` is not
  a payload index (a full scan on a large collection: add the index then). Reverse sets only grow and
  their lifetime is renewed on every put, so the set of a popular chunk never expires; members of
  long-expired entries pile up until that chunk is ingested again (slow memory growth on the free
  256 MB tier). Load tests should include a refresh and measure both.
- **Phase 15.** The refresh and the cache check run on the admin machine only. The pending-finish
  marker is a local file there: production never learns that a run is unfinished (the residual
  window above).
- **Recorded, not changed (Phase 9 audit):** the `namespace` argument of `CacheStore` exists only for
  the owner-run `cache_check.py`; the reverse-set lifetime margin (one hour) and the repair of a
  wrong-type key are builder choices written up as [P]; with no enricher, stored image captions are
  left out of an access-role change (the ingest command always has an enricher, so this cannot
  happen from the command line today); a chunk with no stored text stays "out of date" for ever and
  is counted, never refreshed.

## Chat, session and admin API (Phase 10)
The routes are in `app/api/routes/`. A user signs in (`/auth/...`, Phase 4) and then:

- **Chat:** `POST /chat` with `{"message": "...", "session_id": "..."}` (the session id is optional:
  leave it out to start a new chat; the message is at most 2,000 characters [T]). The answer comes back in one piece (no streaming; the frontend
  shows progress messages while it waits) with citations, warnings and notices, all plain text.
  `503` means starting up or temporarily unavailable, `409` a question is still being answered in that
  session, `429` too many requests or the daily budget (with `Retry-After`), `404` no such session.
  A chat is counted once against the per-user limit, at the route's entry, so a refused one (a bad
  session id, a busy session, the service starting up) is counted too.
- **Sessions:** `GET /sessions`, `GET /sessions/{id}` (resume: the whole transcript, paged),
  `DELETE /sessions/{id}` and `DELETE /sessions` (all my chats). Someone else's session is a 404,
  the same as a missing one.
- **Feedback:** `POST /messages/{id}/feedback` with `{"rating": "up"}` or `"down"`. A thumbs-down
  also adds one item to the admin review queue.
- **Admin (admins only):** users and switching accounts on or off (never an admin), the review
  queue (list, label from a fixed list), document status (counts), the allow-list (list; approve or
  reject only when `ENTITY_REVIEW_ENABLED=true`, and never on Render; a one-word name needs
  `"confirm_single_word": true`), the alerts banner data, and the rollout stage (`GET` and `PUT
  /admin/shadow`).
- **The rollout stage (shadow mode).** One setting with three values: `shadow` (only admins see
  answers), `staff_beta` (admins and staff), `open` (everyone). It starts as `shadow`, and also
  falls back to `shadow` if it cannot be read. A user who may not see answers still has their turn
  generated and stored for evaluation, and gets a fixed notice. Change it with
  `PUT /admin/shadow {"stage": "open"}`; read what was hidden with `GET /admin/shadow/answers`.
  Answers made while hidden stay hidden for that user for good.
- **A withdrawn source.** On resume, an answer whose document was deleted shows "This answer cited a
  document that has since been withdrawn." instead of its text.
- **Page sizes.** Every list has a default and a maximum; a larger value is refused (422), not
  clipped.
- **Start-up.** The API builds the pipeline from the environment (it needs `QDRANT_URL` and the
  gateway keys; without `QDRANT_URL` chat answers 503), starts the name model in the background and
  runs the sparse-vector check once (an alert event `sparse_version_mismatch` on a difference).
  Apply migrations `0006` and `0007` first (`uv run python -m app.db.migrate`). The events of the
  cache, the gateway, the usage limiter and the masker reach the alerts banner.
- **Structure rule.** Nothing outside `app/ingest` imports the ingestion package or a parsing,
  OCR or model library; spaCy and Presidio only inside `app/guardrails` (the masker). A test scans
  the whole `app` tree and a fresh process, before and after the masker loads.
- **A correction to an old note.** The header of migration `0004` says only the owner's command
  switches an account off; since Phase 10 an admin can also do it through the API (never for an
  admin). Migrations are not edited after release, so the note is here and in `0007`.
- **The request size limit** is now 32 KiB [T] (it was 16 KiB): 2,000 characters that a client
  escapes as JSON can reach 24,000 bytes, and the limit must never refuse legitimate text.
- **A local end-to-end check you can run yourself:** `docs/OWNER_RUNS.md`, Run 16.

**Known limitations (recorded in Phase 10)**
- **Phase 11 (frontend).** Staged progress messages while a chat waits (no streaming); the rollout
  stage control and the shadow-answers view; `answer_visible` and `notice` drive what the chat shows.
- **Phase 12 (observability).** The alerts route reads an in-process buffer of the last 1,000 events
  (it is empty after a restart and per instance); the webhook, persistence and dashboards come
  there. Traces of a deleted chat are removed by session hash there too.
- **Phase 14.** The chat route holds a worker thread and a connection per request; load tests must
  size them against the free tiers. The hidden-answers list counts with a subquery per page and
  `messages.shadowed` has no index. `/admin/documents` scrolls the whole collection on every request
  and slices after (counts only, admin only).
- **Phase 15.** Several instances: the per-session lock, the persist queue, the event buffer and
  the 5-second stage cache are per process. A change of stage reaches another instance within 5
  seconds, and an answer given in that window is stored as visible for good (a change from `open`
  back to `shadow` leaks up to 5 seconds on another instance). A proxy in front of the API needs
  the trusted-proxy settings (Phase 4 note). `RENDER=true` as the signal for "on the host" is
  unverified (Phase 15 confirms it).
- **Recorded for the owner (round 2 decisions).** The chat response also carries `kind`, `reason`
  and `answer_visible` and leaves out `heading_path`; the stage read while the connection is
  opened, if a request is cancelled in that very moment, can still be mid-query when the
  connection closes (its error is swallowed and the stage fails closed).
- **Recorded, not built:** an admin cannot delete or ingest a document through the API (the
  command line on the admin machine does that); there is no admin reading of another user's
  transcript except the masked hidden answers.

## Streamlit frontend (Phase 11)
The screens are in `frontend/`. It is a separate service: it installs only
`requirements-frontend.txt` (`streamlit` and `httpx`), imports nothing from `app/`, and needs no
secret. Run it in your own terminal, with the API running:

    uv pip install -r requirements-frontend.txt
    uv run streamlit run frontend/app.py

Settings (names only; set them in your shell): `API_BASE_URL` (default `http://127.0.0.1:8000`) and,
on the admin machine only, `ADMIN_MACHINE=true`. `docs/OWNER_RUNS.md`, Run 17, is a step-by-step look
at every screen.

- **Everyone:** sign in or create a customer account; ask a question and read the answer with its
  sources (document, clause, page), warnings, confidence and reduced-mode notices; the disclaimer
  appears as a footnote only when the service says it applies; thumbs up or down; my chats (resume,
  delete one, delete all, each after a confirmation); a "test period" notice while the rollout
  stage hides answers; a "source withdrawn" notice on old answers.
- **Admins** also get one section at a time: Documents (status; on the admin machine the exact
  ingest and delete commands to copy, never run from the browser), Entities (read-only except on
  the admin machine), Review queue (fixed labels), Users and roles, Alerts (a banner on every
  admin section), Rollout (the stage and the hidden answers) and Evaluation (Phase 13, below).
- **Waiting.** There is no streaming. While an answer is on its way the page shows generic stage
  names and an elapsed timer; they do not claim which step is really running. A service that is
  waking up (a free Render instance) shows "waking up, up to a minute" and carries on by itself.
- **Plain text.** Everything from the API is HTML-escaped before it is shown, so it can never
  become markup or a link. The token lives only in the server-side session (a refresh means signing
  in again) and is never shown, logged or put in a URL.
- **Looks.** Light and dark themes follow the viewer's Streamlit theme, with the system font stack.
  A test checks a contrast of at least 4.5 to 1 for every text colour in both themes. Focus is
  always visible and the layout works at 360 px.
- **The `/chat` response** gained `message_id` (null while the transcript write has not ended); the
  frontend uses it for thumbs.

**Known limitations (recorded in Phase 11)**
- **Phase 12.** The alerts banner reads the API's in-memory buffer; webhook, persistence and the
  dashboards came there (Phase 12). The evaluation page was added in Phase 13.
- **Phase 14.** The sidebar asks the service for the chat list on every page update, and an
  admin section makes two list calls plus the alerts call per update. Bounded by what a person
  clicks; a cache or a smaller call belongs to the load-test round.
- **Phase 13 (done).** "Add to evaluation set" in the review queue works since Phase 13; the
  item is marked added only when the candidate really exists.
- **Phase 15.** Two deploy requirements for `DEPLOY.md`: the Streamlit start command must run from
  the repository root (Streamlit reads `.streamlit/config.toml` from the folder it is started in
  only; from anywhere else the safety settings are off and the page warns), and `API_BASE_URL`
  must always be an https address on Render (the page warns on plain http to another host).
- **Phase 15.** Browser refresh signs you out (the token is held server-side per session by design).
  The Streamlit service's Render settings, health check and the free-tier sleep are written in
  `DEPLOY.md`. A Streamlit session holds one server thread per open browser tab: size it there.
- **Time-based stage text.** The "Searching the documents" and similar stage names change on a
  timer only; they are not progress reports (a decision of the owner).
- **Narrow tables.** A table with several columns (`st.dataframe`) scrolls sideways inside its own
  box at 360 px; the page itself does not. Accepted for the pilot (admin screens).
- **Contrast.** The checked claim is for app-authored markup. Streamlit's own widgets follow
  Streamlit's theme; their captions are forced to the checked muted colour, and the rest is
  checked with a contrast tool in Run 17.
- **Older messages.** `messages.fixed_reply` (migration 0008) is set as messages are written. A
  greeting or refusal stored before it reads as an answer on resume and can show the "not fully
  verified" notice; nothing is guessed to repair them.
- **Theme.** If Streamlit cannot tell the viewer's theme the page uses the light tokens; Run 17
  step 7 checks the switch in a browser.
- **The design paragraphs.** The Phase 11 [P] paragraphs of spec section 24 were written by the
  builder from the design you approved; they are listed for your confirmation before release.
- **Not seen in a browser yet.** The tests use Streamlit's own test runner, which does not draw the
  page: the look, the 360 px layout and the keyboard focus are checked by their CSS rules and by
  Run 17, which you do in a browser.

## Observability (Phase 12)
The parts are in `app/observability/`. Everything there handles names, counts and times; nothing
that enters it holds a question, an answer, a username or a key.

- **Logs.** Every log line is one JSON object with a correlation id (one per request; it is internal,
  never shown to a user, and it is the id of the Langfuse trace too). No request body, header or
  query string is ever logged; only allow-listed fields pass, messages are scrubbed, an exception is
  its class name only. To check a real log file: `uv run python -m app.observability.scan LOGFILE`
  (it prints rule names and line numbers, never the text; Run 12 and Run 18 use it).
- **Traces.** The Langfuse sink also sends the retrieved chunk ids, their content hashes and the
  clause references (a long or odd one is dropped, not cut). Since Change round 1 it also sends the
  masked stored question and answer and the retrieved passages (see the Langfuse sink above;
  `LANGFUSE_SEND_TEXT=false` turns the text off). When a chat is
  deleted (one, all, or by the 90-day sweep) its traces are removed by the keyed session hash, in
  the background, with a persisted queue and bounded retries (5, then one critical alert).
- **Alerts.** Persisted for 30 days and shown in the banner (`/admin/alerts`). `critical` also goes
  to the chat webhook; `warning` is banner only. Rules (error rate, fallback rate, guardrail spike,
  budget variance) need at least 20 requests in the window first; one alert per name per hour;
  at most 20 webhook messages a day. Thresholds are placeholders [T] (`.env.example`).
- **The webhook.** `ALERT_WEBHOOK_URL` is a secret (https only; the secret guard knows its name and
  the Slack, Discord and Teams address shapes). `ALERT_WEBHOOK_FORMAT` is `text` (default) or
  `discord`.
- **Dashboards.** `GET /admin/dashboards/{deciders,keys,usage,cache,ingestion,daily}` and the
  Dashboards section of the frontend: decider in use, key pools, tokens and cost (billed and shadow
  cost apart), cache, ingestion health, and daily customer and staff metrics. A text table beside
  every chart. Live numbers are per process; the saved rollups (`metric_rollups`, counts only) feed
  the daily series.
- **Purge.** `uv run python -m app.memory.retention` (owner-run, never automatic) now also deletes
  audit rows, alert rows and hourly rollups older than 30 days (a pilot value, revisit in Phase 16)
  and writes one `audit_purge` row. Apply migration `0009` first.
- **Dev stage printer.** `DEV_STAGE_TRACE=true` on your machine prints stages, timings and a
  waterfall in the terminal; it is refused on Render.
- **Audit completeness.** `AUDITED_ACTIONS` lists every audited action; a test fails if an audit call,
  a listed action or an admin route that changes state falls out of step.

**Known limitations (recorded in Phase 12)**
- **Phase 15.** The live numbers and the alert rules' windows are per process (one instance), and
  they restart empty after a deploy; the rollups and persisted alerts survive. Several instances
  need a shared window. Scheduling the retention command is part of `DEPLOY.md`.
- **Phase 16.** The alert thresholds, the daily webhook cap, the 30-day audit retention and the
  daily token plan are placeholders until the baseline run.
- **Phase 13 (done).** The evaluation-run audit action and the evaluation dashboards exist
  since Phase 13.
- **Recorded.** Chats deleted before this phase left traces under their hashed session id; there is
  no way to find them without the chat id, so they age out with Langfuse's own retention (set it in
  the Langfuse project settings). Trace deletion uses two Langfuse public routes that Run 18 checks
  against the real service. Rows written before migration 0008 (greetings and refusals) count as
  answers in "verified answers" only when they were grounded and cited, which they never are.
- **Phase 15.** A rollup save whose commit succeeds but whose acknowledgement fails is put back
  and saved again, so one hour's increment can be counted twice (an overcount of at most one
  five-minute batch; the daily rows are recomputed from the tables, so only the hourly sums and
  the dashboards built on them can be off).
- **Phase 14.** A webhook that answers slowly can hold the single alert-writer thread for up to
  its 3-second limit per message. The request path is not affected; a full queue drops alerts and
  only a counter records it (no event).
- **Phase 15.** The retention command and a deactivated account's trace removal need
  `LANGFUSE_USER_HASH_KEY` in the shell; without it only a printed note appears and the trace rows
  are not queued (the DEPLOY.md schedule must set it).
- **Recorded (Phase 12 hardening).** The first audit of Phase 12 did not read the metrics, audit,
  retention, stage-printer, webhook-command, dashboards, secret-guard and event-loop tests. They
  were re-checked by removing each rule in turn; two rules were not guarded (billed against shadow
  cost per answer, and the end bound of the hourly sum) and now are. The `isfinite` test in the
  threshold check is redundant, because the range test already refuses nan and infinity.

## Evaluation (Phase 13)
The parts are in `app/eval/`. It runs on the admin machine, with the evaluation keys, against a
separate test collection and a separate evaluation database (spec 15.1). The hosted API loads only
its light parts (the production sample and the stored numbers); the evaluation libraries are never
loaded by it.

- **The set.** `app/eval/data/cases.json`: 60 cases in 13 categories, 20 active (at least one in
  every category), written from the three sample documents; expected sources are stored by clause
  reference with an anchor phrase, never by chunk id. `calibration.json`: 30 hand-labelled items.
  `uv run python -m app.eval check` tests every rule (including that every case names only
  documents of the sample corpus manifest). **You review all 20 active cases and the 10
  calibration items marked `review`** (the expert-review sample [T]).
- **Runs.** `estimate` (no call: requests and tokens per provider against the free-tier daily
  limits, resumable one-day batches, and a confirmation code), then `run RUN_ID --confirm CODE`.
  A run uses only the evaluation keys, no production budget, limiter or cache, and stops by itself
  when it passes the estimate by more than 25 % (checked between questions; each model call is
  counted and stored as it is made). The code belongs to its own estimate and starts the run
  only within 30 minutes; a run takes one atomic lock, so a second press or a second window
  starts nothing, and a crashed run is free again after 10 minutes without a heartbeat. The edit, delete, conflict and source-removed cases
  change the TEST collection and always undo it (a journal repairs a crash).
- **Scores.** Rules first (behaviour, facts, expected source, leaks, numeric faithfulness), then
  the judge: RAGAS (faithfulness, relevancy, precision, recall, entity recall, correctness) and
  DeepEval (wording judgements) through the gateway's judge call, and typed verdicts that Jev
  decides only for metrics where calibration reached 85 % [T] with at least 5 of 6 verdicts
  given (an outage is a miss), for the Jev and judge models it was calibrated with. A score
  nobody could produce is "not scored" (the reason is kept in the decider field), never zero:
  also a case the system could not answer (unavailable, rate limited, busy) and an answer
  whose model is unknown. The judge may not be of the family of a model that wrote the
  answer, nor of a triage (decide) model. The question a judge sees is masked first with the
  same function that masks a stored transcript.
- **Production.** The running service keeps about 5 % of live answers (masked text and the
  passages that reached the generator) in the separate evaluation database off the request path;
  `judge-samples` scores them here; `drift` writes the weekly report; the retention command
  purges samples after 30 days.
- **Page.** Admin Evaluation section: two views never mixed, a table beside every chart, the
  variance band, per-category scores, drift, calibration, and (admin machine only) estimate and
  start. The review queue's "add to evaluation set" makes a candidate that waits for the next
  dataset version.
- **Commands and runs.** `uv run python -m app.eval --help` (the module docstring lists them);
  `docs/OWNER_RUNS.md` Runs 19 to 23.

**Known limitations (recorded in Phase 13)**
- **Phase 16.** Every per-call size in the estimate, the 25 % margin, the 85 % calibration
  threshold, the 5 % sample rate, the 30-day sample retention, the PSI alert level (0.25) and the
  30 % cost creep are placeholders [T] until the first baseline run replaces them with measured
  numbers.
- **Phase 14 or 16.** The metric inventory of spec 15.4a is built for the floor, RAG, gateway and
  cache numbers that the data allows. Not built: MTTD, MTTR, uptime, incidents and rollback
  frequency, vendor deprecation tracking, trace coverage, index freshness lag, failover success
  rate and circuit-breaker recovery times (they need production history or the Phase 14 chaos
  tests), summary and resume fidelity, staleness and contradiction rates (need a memory-specific
  set), and entity extraction precision and recall (needs hand-labelled entities).
- **Phase 15.** The weekly drift report, the sample judging and the CI evaluation job are commands
  and a job; scheduling them on a free host is part of `DEPLOY.md`. The CI job uses a scripted
  model, so it checks the harness, not the model.
- **Phase 16.** A run answers with the Groq and Gemini evaluation keys only; the OpenRouter
  (Gemma) fallback answers are not evaluated (the OpenRouter evaluation keys are the judge's).
  Keep this in mind when reading the baseline.
  Jev counts against its own dedicated key and its paid tokens, and so appears in the estimate.
- **Recorded.** A run started from the command line is recorded in the evaluation database
  (`eval_runs`: who, when, the confirmed estimate), not in the main audit log, because an
  evaluation never writes to the main database (build rule 3). A run started from the page also
  writes an `evaluation_run` row for its estimate and its start.
- **Recorded.** `ragas 0.4.3` has an advisory (the multi-modal faithfulness metric) and its
  `diskcache` dependency another, both without a fixed release; neither is used here
  (`requirements-eval.txt` has the reasoning). CI's pip-audit does not cover the evaluation file.
- **Recorded.** Customer questions that become candidates carry the masked text the review item
  holds, masked once more by the application's masker; a candidate can join the set only when
  its document is in the sample corpus manifest.
- **Phase 16.** A candidate from the review queue is always role `customer`, whatever role
  asked it; a staff question is therefore replayed as a customer's.
- **Phase 16.** The production Jev key, the embedding keys and the rerank key are shared with
  production and so are their daily limits: an evaluation run's calls count against them (the
  estimate says so, and the day's limit check counts every run's use of them). No `EVAL_`
  variant of these keys exists by decision.
- **Phase 16.** The made-up name in case p05 is found only by the NER model; the answer a
  production sample holds is the stored copy, which can differ from what the user saw by the
  fixed unclear-ending text; the staff-role document text lives in the evaluation database
  (accepted: it is the sample corpus).
- **Phase 16.** The usage of a case is checked against the day's limits per whole batch before
  it starts, not per case; one case can overshoot the daily limit of a provider by its own size.
- **Owner question (spec 15.3).** The spec says calibration measures "agreement with the judge
  and the labels"; the build stores both and trusts Jev on its agreement with the LABELS only
  (Jev against the judge is for information). Say if the other reading was meant.
- **Phase 13 hardening.** The adapter tests (RAGAS, DeepEval) run only in the throw-away
  environment that has `requirements-eval.txt`; CI does not install it. `claim_supported` and
  `injection_call_right` are calibrated but not yet used by a run. Injection text inside a
  passage can reach RAGAS's and G-Eval's own prompt templates; the typed judge calls carry a
  "data, not instructions" note, the library templates cannot.
- **Phase 16.** Edit and supersede scenarios re-embed the changed clause when a re-embedding
  call is possible (one embedding request per change, counted in the estimate); without an
  embedder they change the lexical (sparse) vector only.
- **Not seen with real models yet.** The 60 cases and the stubbed-model run prove the harness; how
  the real system does on them is Run 21. Names of people in the made-up PII cases are masked by the
  NER model, which only a real run exercises.

## Testing completion (Phase 14)
What each kind of test is for, where it lives and how to run it. None of it needs a key or a
network, except the two owner-run steps marked below.

- **Chaos tests** (`tests/test_chaos_gateway.py`, `tests/test_chaos_pipeline.py`, marker `chaos`).
  Schedules of failures over time on a fake clock: every provider failing, the breakers opening,
  one probe after the open period, healing; slow calls against the per-call timeout
  (`GATEWAY_TIMEOUT_SECONDS`, default 10 s): 8 s is a success and never trips a breaker, 15 s is
  cut at 10 s and counts as a timeout failure, and with the setting at 30 s both 15 s and 25 s
  succeed; slow failures that must leave each next attempt only the time that is left; 429 with
  `Retry-After` (a key stays out until the time the header asked for, probed at about 100 s of a
  120 s header; 0 is raised to the floor); Jev, embeddings and the rerank chain failing and
  returning. Seen from the user: a clean answer or the one fixed "temporarily unavailable" text,
  nothing stored, and the next request recovers. One test puts the REAL gateway on a fake clock
  under the pipeline; the other pipeline tests use the scripted gateway (with real, short sleeps).
- **Contract tests** (`tests/test_contract_schemas.py`, marker `contract`). A table of good and
  bad payloads for each structured output (typed decisions in Jev's shape, a chat model's JSON,
  the generator's answer, embedding vectors, rerank responses): a bad one is a typed gateway error,
  never a value.
- **Recorded tests** (`tests/test_recorded_providers.py`, marker `recorded`) replay five
  recordings of real provider answers (Cohere, Jina, Jev, Gemini embeddings, OpenRouter) through
  the real client code with `record_mode="none"`. **The recordings are made by you** with
  `scripts/record_cassettes.py --run` (Run 25); until then these five tests are skipped with a
  plain message. Hygiene (`tests/test_cassette_hygiene.py`): headers and `key=` parameters are
  replaced, response headers dropped, embedding vectors cut to 8 numbers (the length is kept and
  restored on replay), long random-looking tokens replaced, and a detector refuses any file with a
  key-shaped value or a long high-entropy string. Run gitleaks on the folder before committing it.
- **Coverage** (`scripts/coverage_report.py`, CI job `coverage`, alert only). Target 80 % of lines
  on gateway, guardrails, cache and memory (measured 97 to 99 % when this phase was built);
  accounts, api, pipeline and ingest are reported for information. The CI job runs with the
  throw-away Postgres container so the Postgres tests count. Run it here:
  `uv run pytest -q --cov=app --cov-branch --cov-report=json:coverage.json` then
  `uv run python scripts/coverage_report.py coverage.json`.
- **Load tests** (`load/`, Locust). The accounts of the load users are made by the server process
  straight in its local SQLite file with the application's own account functions
  (`load/seed.py`): the per-address registration limit stays on and is never what is measured;
  users only log in (the per-address LOGIN limit is switched off in the load servers, since every
  virtual user shares one address). Two modes. `stub`: `load/stub_server.py` is the real API with a
  scripted model, the sample corpus in an in-memory store and SQLite behind it. `real`:
  `load/real_server.py` is the real API with your providers, and everything it WRITES is local (a
  SQLite file in a temporary folder, an in-process Redis stand-in); the cloud vector store is only
  READ, through a wrapper that refuses every write call, and only an `_eval` collection. It
  refuses a cloud `DATABASE_URL`, `UPSTASH_REDIS_URL` or `EVAL_DATABASE_URL`, any other collection
  name, a non-loopback host, and it needs the single-use ticket that `scripts/load_estimate.py`
  made for that plan: bound to the plan and the UTC date, valid for 30 minutes, used up when the
  real server starts (Run 27). Users step up from 10 (default 10, 20, 40, 80, 160, a step is at
  least 20 s). `load/report.py` reads Locust's history (run it with `--csv-full-history`: only
  then are the percentiles the current ~10 s window and not the whole run so far; the report
  warns when they are cumulative), assigns rows to steps by TIME, and a step breaks at more than
  1 % failed requests, more than 1 % of chat answers "temporarily unavailable" (HTTP 503
  `service_unavailable` is a failure; only 429 `too_many_requests` and the hasher's busy at login
  are expected rejections), a p95 above 30 s, or fewer than 80 % of the step's users active. The
  report always states that a stub run shows how the code behaves under concurrency, not host
  capacity. Runs 26 and 27 are in `docs/OWNER_RUNS.md`.
- **Known blind spots of a load run (recorded for Phase 15).** The API opens one database
  connection per chat request and the stub uses SQLite, so a cloud Postgres connection limit is
  not exercised. **Phase 15 decides** whether the running service uses the provider's direct
  address or its pooled one, and checks the connection count on the deployed service. Host
  capacity (Render's free instance) is measured there with a short real run you approve (Phase
  15/16).
- **Phase 16.** The step sizes, the 1 % failure share, the 30 s p95 and the 80 % coverage target
  are placeholders for the baseline; the 30 s total request budget [T] is recalibrated only from a
  real run you approved.
- **Known limitations (Phase 14).** The load servers loosen one thing on purpose: the per-address
  LOGIN limit. The per-user rate limit and a free tier's own limit appear in a real run as
  `limited` in the report, counted apart from failures. A cassette is a snapshot: a provider
  that changes its answer shape is seen only when you record again, and matching ignores request
  bodies. `extract_json` takes the first `{...}` of a prose answer, so an earlier brace hides a
  later object. The stub server imports the test fixtures (`tests.eval_rig`), so it is a
  development tool only. `yaml` (used by two tests) arrives with vcrpy and is not listed on its
  own. Whether VCR intercepts LiteLLM's HTTP client for Gemini and OpenRouter is first shown by a
  real recording (Run 25). The timing test cannot see a wrong value at a node that did not run
  (a node skipped for lack of time is by design). Locust's own percentile semantics were read
  in its source (`stats.py`): with `--csv-full-history` the Aggregated row uses the current
  window, without it the cumulative values.

## Deployment (Phase 15)
Nothing here was deployed by Claude: it is the files and the guide for YOU to deploy. The guide is
`docs/DEPLOY.md` (click by click, with what you should see); every real step is a run in
`docs/OWNER_RUNS.md` (Runs 28 to 34).

- **`render.yaml`.** The blueprint: `bfsi-api` and `bfsi-frontend`, free plan, health checks
  `/live` and `/_stcore/health`, start commands from the repository root (so `prompts/` and
  `.streamlit/config.toml` are found), `autoDeployTrigger: checksPass` (Render deploys by itself
  after the CI checks pass: no Render secret in GitHub). This is the one gate and a documented
  exception to the rule that no check blocks a release: a failing check holds only the AUTOMATIC
  deploy, and Render's **Manual Deploy** releases any commit regardless (DEPLOY.md section 3).
  Every secret is `sync: false`: a name, never a value. The region is a marked placeholder you
  fill in (Run 28).
- **The client address behind Render's proxy.** The API still reads only the socket's address
  unless `TRUSTED_PROXY_HOPS` is set (default 0). With N hops the entry N from the end of
  `X-Forwarded-For` is used (each trusted proxy appends what it saw; what a client wrote earlier is
  never believed). uvicorn starts with `--no-proxy-headers` so only our own code reads the header.
  **`render.yaml` ships 0** until Run 28 has looked at what Render really sends: `GET
  /admin/diagnostics` (admin only) shows the number of header entries, `address_at_one_hop` and
  the process memory; only when `address_at_one_hop` is your real public address do you set 1.
  At 0 every user shares one per-address limit (safe, only coarse).
- **The spaCy model wheel is pinned by SHA-256** in `requirements.txt` (`#sha256=`): pip and uv
  refuse a different file. The value was computed from one download of the release asset.
- **The pinger** (`.github/workflows/keepalive.yml`) pings both services only inside 08:00 to 20:00
  India time on weekdays (free hours are about 750 a month for all services together [T]). GitHub's
  scheduled workflows are best effort and are switched off after 60 days without repository
  activity. **After a deploy**, the separate workflow `After a deploy` (`post-deploy.yml`) asks
  `/live`: an alert on `/live` only, which cannot tell the new build from the old one. It is a
  separate workflow so that a failure while the service is down cannot hold the fix (Render waits
  for every check of a commit). The pinger and this check FAIL VISIBLY (a red run), and block
  nothing.
- **Backups.** `scripts/qdrant_snapshot.py` (list, create, download by name or `--newest`, prune,
  restore-test; `--run` for anything that changes). **The tested route is the re-ingest from
  `originals/`**, whose own backup you keep (the folder `originals/_withdrawn/` included) and
  whose restore you test once (`docs/runbooks/restore-documents.md`). Restoring a downloaded
  snapshot is **unproven** until Run 30 has restored one into the throw-away collection
  `documents_restore_test` and counted it. The weekly snapshot task is create, download, then
  prune (prune deletes only what has a downloaded copy).
- **A withdrawn document stays withdrawn (migration 0010).** The delete command records the id,
  the time and a fixed reason code in the permanent table `withdrawn_documents` (no text),
  moves the PDFs to `originals/_withdrawn/<id>/`, and an ingest of that id is refused (a new
  document needs a new id). A restore ends with `uv run python -m app.ingest withdrawn`, which
  lists every withdrawn id and says whether any is still stored, then `delete` again for each
  one that is. Snapshots taken before a withdrawal still hold its text until they age out (known
  limitation). A document withdrawn before the table existed is entered by running `delete`
  again (Run 34 rehearses the whole chain on the test collection).
- **Where admin jobs are done.** The hosted frontend, signed in as the admin, serves Rollout,
  Users, Documents counts, the Review queue, Alerts and the dashboards. Ingest, delete, restore,
  snapshots, retention and entity approval are commands on the admin machine; a local frontend
  pointed at the Render API (`ADMIN_MACHINE=true`) only shows their words (the hosted API refuses
  entity approval; `DEPLOY.md` 2.5b).
- **Runbooks** (`docs/runbooks/`, with an index and a rehearsal column): all keys down; wrong
  answer reported; document withdrawn; suspected personal data; key leaked; bad deploy (go back);
  restore the documents. **Key rotation**: `docs/KEY_ROTATION.md` and
  `scripts/key_inventory.py` (names, counts and eight-character fingerprints only).
  **Data flow**: `docs/DATA_FLOW.md`. **Release records**: `docs/releases/` and
  `scripts/release_record.py`. **Cost**: `docs/COST_PROJECTION.md` and
  `scripts/cost_projection.py`.
- **Database address.** The running API uses the DIRECT address for now. The pooled address is not
  recommended until a deliberate test on a real pooled connection checks the session isolation
  setting and the start-up options. `scripts/db_connections.py` (Run 32) measures the connection
  count first.
- **Scheduled jobs** run on the admin machine from Windows Task Scheduler (Render's free plan has
  no cron): retention (daily), the drift report (weekly), the snapshot (weekly). The retention task
  now writes one `retention_sweep` audit row on every real run, also when it deleted nothing; the
  service raises the warning `retention_overdue` when the newest is more than seven days old
  (the task runs daily). Judging the sample
  stays manual (it needs the code of its own estimate).
- **The first deploy is in the `shadow` stage** and stays there until the checklist of
  `DEPLOY.md` section 6 is true: moving on is a deliberate step. The database you use may not
  be new: `DEPLOY.md` 2.8 reads the stage and sets it back.

**Known limitations from the second audit of Phase 15 (recorded, not simple text fixes)**
- **Phase 16.** The first public exposure has one shared limit until Run 28 step 2.6 is done
  (an outsider could use up the sign-in allowance for a while); the risk note is in Run 28.
- **Phase 16.** The drift and snapshot scheduled tasks have no alert when they stop (only
  retention has `retention_overdue`); their logs and Task Scheduler's last result are the check.
- **Phase 16.** Postgres (accounts and chats) has no backup of its own beyond what the Neon plan
  keeps; there is no export step.
- **Phase 16.** The snapshot size check trusts a server that reports a size; a server that gives
  no size leaves the snapshot unpruned (kept), by design.
- **Phase 16.** Render's menu names, region words and the Auto-Deploy-after-Rollback behaviour in
  `DEPLOY.md` are unverified [T] and are checked on the day of Run 28 and Run 29.
- **Phase 16.** There is no admin command that removes one named chat or one evaluation sample
  (the suspected-personal-data runbook says who to ask).

**Known limitations (recorded in Phase 15)**
- **Phase 16.** Render's free-plan numbers (750 hours, 512 MB, sleep after about 15 minutes) and
  the blueprint key `autoDeployTrigger: checksPass` are stated from memory and marked [T]; the
  header Render adds and `RENDER=true` as the signal for "on the host" are confirmed by Run 28.
- **Phase 16.** The persist queue and every in-process number (the live counts, the alert windows,
  the 5-second stage cache, the session locks) reset at a restart; a queued transcript write is
  lost if the service restarts before it is written. One instance only.
- **Phase 16.** The post-deploy check cannot tell the new build from the old one (the liveness
  answer reveals nothing by design). The first-deploy memory number is a single measurement
  (Run 28).
- **Phase 16.** The 90-day, 30-day and 7-day periods, the 08:00 to 20:00 window and the 80 %
  checklist items are placeholders for the baseline.
- **Owner decision recorded.** `EVAL_*` names are not all kept off Render: `EVAL_DATABASE_URL`
  and `EVAL_SAMPLE_RATE` are set there on purpose (the masked production sample); the evaluation
  keys, the judge model and the test collection names never are.

## Baseline, targets and the pre-launch gate (Phase 16)
Phase 16 is mostly measurement, so what is built here is the tooling that turns your runs into
targets and into one honest report. **Nothing was measured by Claude**: the numbers come from
your Runs 20 to 22 and 35 to 38, and until they exist every target stays "baseline" and the gate
says NO EVIDENCE. All of it is **alert only** (build rule 5): nothing blocks a merge, a release
or a stage change.

- **Targets** (`docs/TARGETS.md`, `uv run python -m app.eval targets`). Four rules: a
  **regression floor** (baseline mean minus the variance band), a **ceiling** for latency p95
  and tokens per query (baseline plus 20 %), an **absolute floor** you sign for the
  safety-critical measures (citation validity, faithfulness, refusal correctness, access
  isolation), independent of the baseline, and a **zero rule** (PII leak, cross-role leak, prompt
  leak, stale answer: any non-zero is an alert). The command prints proposals and writes
  nothing; a signed floor lives in `app/eval/data/targets_signed.json` with a name and a date.
- **The red-team set** (`app/eval/data/redteam.json`): 40 adversarial cases, 20 development and
  20 held-out, separate from the 60-case set. `python -m app.eval estimate --set redteam_dev` or
  `--set redteam_heldout` gives each its own estimate and code. **The gate is judged on the
  held-out half only.** No real data.
- **The readiness score** (`docs/READINESS.md`): five categories of 20 points; **a line with no
  cited evidence scores 0**; the scoring is conservative and is re-scored by a second review.
  Terms of service: one key per provider from one account; the key pool is not for pooling free
  quota, and capacity plans (`app/eval/estimate.py`, `ONE_KEY_PER_PROVIDER`) count one key.
- **The gate and the stage checklists** (`scripts/prelaunch_gate.py`, `--stage staff_beta` or
  `open`): PASS, FAIL or NO EVIDENCE for each of the nine items of spec 17.6, from the readiness
  page and `docs/prelaunch_evidence.json`. `--write` saves the report for the release record.
- **Cost against a dry run** (`scripts/cost_dry_run_compare.py`).
- **Access-control audit** (`docs/ACCESS_CONTROL_AUDIT.md`, `tests/test_access_control_matrix.py`):
  every route is asked four ways (no token, customer, staff, admin) and compared with a table; a
  new route has to be decided. **An independent second review is required before any real
  customer data.**
- **Security scan** (`docs/SECURITY_SCAN.md`): pip-audit of all five requirements files (CI does
  two), bandit (new, alert only in CI, with `.bandit-baseline.json`), gitleaks. One finding was
  fixed rather than accepted: literal direction-control characters in `frontend/render.py`
  (`tests/test_source_hygiene.py` keeps them out).
- **Decided values** recorded in the spec: staff token budget 200,000, review overdue limit
  2 working days, shadow period two weeks. **To be named by the owner:** queries per day, the
  monthly budget ceiling, the business owner.

- **Round 2 (hardening).** Every recorded pass carries a date and goes **STALE** after 30 days (7
  for the two stage items that say "last 7 days"); a missing or unreadable evidence file says so;
  the report prints, every time, that the evidence is self-attested. Held-out red-team failures are
  shown as a count, never as ids. The admin page's estimate never includes red-team cases. A band
  larger than the mean is "no usable target", never a clamped number. Every B608 site is listed in
  `docs/sql_sites.json` and a new one fails `tests/test_sql_sites.py` until it is read.
  `GEMINI_EMBEDDING_KEY_B` is a rotation overlap of the same account, not a second account.

**Known limitations (recorded in Phase 16)**
- **Phase 16 runs.** Until Run 21 exists there is no baseline and no band; every target is
  "baseline" and the gate has nothing to judge. The pass threshold of the held-out half and the
  absolute floors are yours to set; the tools never invent them.
- **Phase 16 runs.** A run id in the evidence file is checked by its shape only (the run lives in
  your evaluation database); a file, test or page citation is checked to exist.
- **Later.** The red-team cases test what an attacker types; they are not a penetration test.
  Indirect injection through a document is covered by the ingestion scan and its own tests, and
  by one case in each half.
- **Phase 16 runs.** The evidence file and the readiness page are typed by a person: the gate
  refuses an empty, undated, stale or inconsistent claim and cannot prove a true one; the second
  review is where claims are checked. A `file:` citation proves existence only.
- **Later.** The no-shared-question rule folds case, punctuation and spaces; a reworded
  near-duplicate between the halves is for the second review to catch.
- **Later.** The static-scan comparison counts findings per file and rule; a swapped finding of
  the same rule and text in the same file is not reported (B608 is closed by the site list).
- **Later.** The route matrix tests the answer to a token; it does not test rate limits or
  token budgets (their own tests do).
- **Later.** The bandit baseline accepts the f-string SQL findings because every value is a bound
  parameter; a future query that puts a value into the text would be a NEW finding of the same
  kind and could hide among the accepted ones. Read a new B608 with that in mind.

## Rules in plain words
- Tests and evaluations never touch production data.
- Ask the owner before any load test or evaluation that uses real provider
  keys, because it can rate-limit every model.
- Quality checks raise alerts. They never block a release by themselves.
- No real customer data until paid or zero-retention plans are in place.

## More documents (created later)
- `DEPLOY.md` : click-by-click deployment steps (Phase 15).
- Runbooks : what to do when keys fail, a wrong answer is reported, a
  document is withdrawn, PII leaks, a key leaks, or a deploy goes bad
  (Phase 15).
