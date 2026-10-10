# BFSI RAG Assistant

A read-only question-answering assistant over a company's banking, financial
services and insurance (BFSI) policy and regulatory documents. Every answer is
grounded in the documents and cites clauses. Customers, staff and admins see
different documents.

Current phase: Phase 8 built, awaiting the owner's approval (next is Phase 9)

## Where things are
- `spec/` : the full specification (`build.md`), two diagrams and three
  sample company PDFs. This is the source of truth.
- `CLAUDE.md` : the rules every Claude Code session reads.
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
- `requirements*.txt` : the four dependency files. `.env.example` : the settings template.
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

**(f) The delete test.** `uv run python -m app.ingest delete --doc-id insurance-flexi-property` asks
you to type the id. You should see `insurance-flexi-property: removed 19 chunk(s), N allow-list
entr(ies); 0 chat message(s) flagged 'source removed'`. Then `status`: `stored 0`,
`allow-list entries: none`, marker `no`. Then ingest it again (same command as in (b)): `new 19 ...
version 1`. It is much quicker and makes far fewer model calls, because the answers are saved in
`originals/.cache`.

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

1. Delete it (type its id): `uv run python -m app.ingest delete --doc-id banking-rtl-facility`
2. Remove the saved answers so the picture is asked for again: `Remove-Item -Recurse -Force originals\.cache`
3. Break the Gemini key for this window only (the caption has no other provider):
   `$env:GEMINI_API_KEYS = "not-a-real-key"`
4. Run the banking ingest command again. You should see `document 'banking-rtl-facility' was NOT
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
  confident product line or clause type narrows the search; if that finds nothing it is repeated
  without. Rerank keeps the top 5.
- **Answers** (`answer.py`): the generator gets the instructions, the labelled chunks and the
  question, never the chat history. A failed number or citation check still returns the answer, with a
  warning and the unchecked numbers marked. Low confidence (best score under 0.40) answers with a
  statement that it may be wrong and goes to the review queue; no usable chunk gives the fixed refusal.
- **Time**: 30 seconds for the whole request. Each step gets the smaller of its own limit and the time
  left, and a step that cannot finish is not started.
- **Safety** (`safety.py`, `startup.py`, `locks.py`): LangChain / LangSmith tracing is switched off in
  code and the graph is compiled with no checkpointer (the state holds the raw question); the masker
  loads in the background and until it is ready the answer is "starting up"; one turn at a time per
  session; a failed dependency gives "temporarily unavailable", never an ungrounded answer; if
  Postgres fails the answer is returned and the masked turn is queued in memory.
- **Result** (`state.Result`): kind (answer, reply, refusal, reword, unavailable, starting_up, busy,
  rate_limited, over_budget, session_not_found), text, citations, warnings, confidence, degraded
  notices, whether the disclaimer was added, per-stage timings, tokens and an audit dict of ids and
  hashes (no text).
- **A real-service check you can run yourself**, in your own terminal (it asks one question through
  all 17 stages and prints names, timings and counts only, never the question, the answer or a key):
  `uv run python scripts/pipeline_smoke.py --run` (add `--role staff`, `--question "..."` or
  `--with-cache`). It uses your real keys and Qdrant, a throw-away SQLite file for the chat tables,
  and never opens your real database. Nothing happens without `--run`.
- **Langfuse sink** (`app/pipeline/langfuse_sink.py`, **[P]**). Off by default. It turns on only when
  `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` and `LANGFUSE_USER_HASH_KEY` are all
  set to real values (the last must be its own key, not `JWT_SECRET_KEY` or `AUDIT_HASH_KEY`).
  It sends span records only: stage names, timings, decider, model, key fingerprint, token counts,
  costs, flags, prompt hashes, corpus version, and errors as exception type names. It never sends the
  question, the answer, chunk text, history, summary, username or email; the user and session appear
  only as keyed hashes. It uses Langfuse's plain ingestion API over httpx, not the SDK (the SDK can
  capture inputs and outputs automatically). A Langfuse failure or slowness cannot touch an answer:
  background batches, a 3 s timeout, a bounded queue of 200 that drops when full, sent after the answer
  is final. Alert events: `langfuse_failed`, `langfuse_dropped`, `langfuse_disabled` (type names only,
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
  live in one process (one Render instance); several instances would need a shared lock.
- **Phase 12 (observability).** The Langfuse sink exists (Phase 8); Phase 12 builds dashboards,
  retention and evaluation on top and may later decide whether masked text is sent. The alert
  events are plain callbacks: `stage_failed`, `persist_queued`, `persist_queue_overflow`,
  `persist_masking_failed`, `masker_load_failed`, `masker_unavailable`, `cache_entry_discarded`,
  `cache_write_skipped`, `vector_store_rejected`, `triage_failed`, `triage_probability_invalid`,
  `rewrite_failed`, `rewrite_unreadable`, `rewrite_rejected_injection`, `rerank_failed`,
  `allowlist_unavailable`, `session_unavailable`, `request_budget_exhausted`, `graph_failed`,
  `tracing_switched_off`, `summary_rejected_injection`, `summary_fold_failed`, `budget_charge_failed`.
- **Phase 14 (testing completion).** Masking runs on the event loop thread and takes about 20 ms; load
  tests must show whether that matters. A step that times out leaves its worker thread running until
  it ends (the answer is already returned).
- **Phase 15 (deployment).** The persist queue is lost if the host restarts. Deploy `prompts/` next to
  `app/`.
- **Phase 16 (baseline run).** Every number in spec 4.5 is a starting value: the 0.30 and 0.50 floors,
  0.40 low confidence, 0.60 out of domain, 0.80 soft filters, 0.50 history, the node timeouts and the
  3,400-token budget estimate. Also to review there: the keyword rules of the no-LLM triage, whether a
  bare follow-up should be checked for domain, the adjacent clause for neighbour context, and the
  optional small caches.

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
