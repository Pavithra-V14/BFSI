# BFSI RAG Assistant

A read-only question-answering assistant over a company's banking, financial
services and insurance (BFSI) policy and regulatory documents. Every answer is
grounded in the documents and cites clauses. Customers, staff and admins see
different documents.

Current phase: Phase 6 built, awaiting the owner's approval (next is Phase 7)

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
  measured against them. `normalise_entity` also trims `; : ( ) [ ]` and a closing "." of a stored
  term (for example "Pvt. Ltd." is stored as "Pvt. Ltd"): masking must use the same function.
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

1. **Local scan** (`app/guardrails/scan.py`): at most 100 tokens (never cut silently); a first
   message of at least 10 characters (a follow-up only has to be non-empty); a whole-message greeting
   or farewell gets a fixed reply; the injection regex is recorded; names, phones, emails and
   identifiers (PAN, Aadhaar, cards, policy and account numbers) are replaced by `[PERSON]`,
   `[PHONE]`, `[EMAIL]` and `[ID]` before any model sees the question. Places are not masked.
2. **Injection decision**: a strong regex hit or a Jev score of 0.85 or more blocks; a weak hit or a
   score from 0.50 asks the user to reword. These numbers are starting values for the Phase 16
   baseline.
3. **Context guard**: drops chunks below a relevance of 0.30 and chunks that read like instructions
   to the model; if nothing is left the answer is a fixed refusal. What is left goes into the prompt
   between clear delimiters that a chunk cannot forge.
4. **Output guard**: the answer must be JSON with citations; every number, percentage, date and
   amount must appear in the cited chunks; every citation must be a document the model was given; the
   words of the answer must be found in the cited text (0.6 to start). If a check fails the answer is
   still returned, with a warning and the unchecked numbers marked, and it is not cached.
5. **Plain text**: model output is shown as plain text. HTML and markdown link syntax are removed; a
   web address stays only when its domain is a regulator's or the company's (`COMPANY_DOMAINS`).
6. **Disclaimer**: added only for advice-like questions or answers with a financial figure.

**Prompts** are files in `prompts/` named `name.v1.md`, each with the same sections (role, task,
constraints, output format, a good and a bad example, a refusal rule). Changing a prompt means a new
file or a new version; its SHA-256 hash is recorded with every answer. The image-caption and
table-summary prompts used by ingestion live there too, word for word as before.

**Known limitations (recorded in Phase 6)**
- **Phase 16 (baseline run).** The small spaCy model (`en_core_web_sm`) has limited recall for Indian
  names: it misses some, and names typed in capitals are often missed. The name patterns catch "my name
  is ...", "I am ..." and "Mr. ...", but a bare name can pass. All thresholds above are starting values.
- **Phase 8 (pipeline) and Phase 15 (deployment).** The first masking call loads the model and takes
  about 40 seconds on the admin machine (about 22 ms afterwards), so the API must warm it at start-up.
  The model needs about 140 MB of memory in production.

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
