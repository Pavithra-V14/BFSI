# BFSI RAG Assistant: project rules

The full specification is `spec/build.md`, with diagrams `spec/architecture.png`
and `spec/query_workflow.png`. Read the relevant section of the spec before
working on it. This file does not repeat the spec; it holds the rules that every
session must follow.

## Source of truth
- `spec/build.md` is the only source of requirements. Build only what it says.
- Section 22 of the spec lists what is out of scope. Do not add any of it.
- Legend in the spec: untagged = decision; **[P]** = proposed, build as written
  but confirm with the owner before release; **[T]** = placeholder, ask the
  owner for the real value before it matters.
- If the spec is silent or contradicts itself, ask the owner. Do not guess.
- Numeric quality targets stay "baseline" until the first baseline evaluation
  run (spec section 15).

## Build rules (from the "Build rules" block at the top of spec/build.md)
1. Build only what the spec says.
2. Never print, log or commit a secret value (API keys, tokens, passwords).
   Log only a short fingerprint of a key.
3. Never run tests, evaluations or load tests against production databases or
   the production vector collection. Tests refuse to run if a production URL is
   configured.
4. Ask the owner before any load test or evaluation run that uses real provider
   keys.
5. All quality gates are alerts only. No gate blocks a merge or release.

## Owner instructions (not in the spec)
- Follow the build order in spec section 21 (itself tagged [P]).
- Stop for the owner's approval after each phase. Never start the next phase
  unprompted.
- Do not run any hook script against the owner's real files without telling
  the owner first.
- Never read, print or write `.env*` files or key files. `.env.example` holds
  variable names and comments only, never values.
- The owner is on Windows. All helper and hook scripts are Python, saved as
  UTF-8 without a byte-order mark. Use forward slashes in configuration paths.
- Use uv to run Python tools (`uv run pytest`, `uv run ruff`) and to install dependencies from the requirements files (`uv pip install -r requirements.txt`). The requirements files stay the single source of dependencies, as the spec says: do not use `uv add` or pyproject-based dependency management unless I decide otherwise.
- `.venv/` and `.env*` (except `.env.example`) are in `.gitignore`; keep them there, and keep `.venv/` out of any review.
- The Streamlit frontend must be beautiful and polished, with an outstanding UI and all
  required features from the spec. Before building it, propose a design (palette,
  typography, layout, light and dark theme, main screens, component list) for the owner's
  approval. App-authored markup may use custom CSS and HTML; model output is always
  rendered as plain text (spec invariant), never as HTML or links.
- Postgres is cloud-only (Neon or Supabase); no Postgres runs on this machine.
- The owner is new to deployment. Explain in plain words (spec section 17.4).

## Stack (spec section 2)
| Concern | Choice |
|---|---|
| API | FastAPI |
| Frontend | Streamlit (separate service) |
| Orchestration | LangGraph |
| LLM access | LiteLLM as a library inside our own gateway (no Router) |
| Vector store | Qdrant (dense + sparse in one collection) |
| Database | Postgres (Neon); SQLite in unit tests |
| Cache | Upstash (Redis protocol) |
| Migrations | Numbered SQL files with a small runner |

Four separate dependency files: `requirements.txt` (runtime only),
`requirements-ingest.txt`, `requirements-eval.txt`, `requirements-dev.txt`.
Ingestion, entity extraction, evaluation and load tests run locally only.
Production (Render) serves queries only.

## Invariants that are easy to break
- Every model call goes through the gateway module. Nothing else calls a
  provider directly.
- The generator never sees session history.
- Only masked text appears in logs, traces, caches and stored transcripts.
  PII masking happens locally before any LLM call.
- The `access_role` and `effective_to` filters are applied on both the dense
  and the sparse search branches.
- Chunk identity depends on content and clause number, never on position or
  page number.
- Only grounded, verified answers with no warning are cached.
- Render model output as plain text. Strip HTML and links.
- The hash-based fallback embedding is for development only and must never
  index or retrieve in production.
- Eval keys are never installed on Render. Jev has its own dedicated key.
- Where a rule can be code instead of a prompt instruction, it is code.

## Commands
<!-- Kept up to date as phases add commands. Items marked TBD are not
     decided yet; do not invent them. -->
- Create environment (once, Phase 1, after owner approval): `uv venv`
  (creates `.venv` in the project root)
- Install runtime: `uv pip install -r requirements.txt`
- Install others: `uv pip install -r requirements-dev.txt`
  (likewise `-ingest`, `-eval`)
- Test: `uv run pytest`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .`
- Run API / Streamlit: TBD (until the app exists)
- Apply migrations: `uv run python -m app.db.migrate` (add `--dry-run` to list pending
  files). It reads the database URL from `DATABASE_URL` in your shell, never from a file or
  the command line. Use the provider's direct (non-pooled) URL with `sslmode=require`.
- Tests refuse to run if a database, Qdrant or cache URL points at a remote host (isolation guard).

## Definition of done for a phase (proposal, not in the spec)
- Unit tests for the phase pass using the isolation guard (no production URLs).
- No secret values in the diff or in any output.
- A short summary lists the files created or changed and what each does.
- Anything tagged [P] or [T] that the phase touched is listed for the owner.

## Where to look
- Pipeline stages and early exits: spec section 4, `spec/query_workflow.png`.
- Gateway: section 5. Ingestion: section 6. Retrieval: section 7.
- Memory: section 8. Guardrails: section 9. Cache: section 10.
- Security: section 13. Evaluation: section 15. Testing: section 16.
- Test corpus: section 20 and `spec/samples/`.
