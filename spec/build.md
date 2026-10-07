# BFSI RAG Assistant — Project Specification

This document is a complete, self-contained specification. A team (or an AI coding agent) can build the whole system from it without any other document.

**Diagrams (same folder as this file)**
- `architecture.png` — components, data stores, external services, admin machine, operations.
- `query_workflow.png` — the 17-stage query pipeline with its early exits.
- The `.svg` files are the editable sources of the same two images.

**Legend**
- Untagged statements are **decisions** made by the project owner.
- **[P]** = proposed during design and not explicitly confirmed. Build it as written, but confirm with the owner before release.
- **[T]** = to be decided / a placeholder value. Ask the owner for the real value before it matters.
- Numeric quality targets are set **after the first baseline evaluation run** (section 15). Until then, targets are recorded as "baseline".

**Build rules**
1. Build only what this document says. Do not add features that are not listed. Items in section 22 are explicitly out of scope.
2. Never print, log or commit a secret value (API keys, tokens, passwords). Log only a short fingerprint of a key.
3. Never run tests, evaluations or load tests against production databases or the production vector collection. Tests refuse to run if a production URL is configured.
4. Ask the owner before any load test or evaluation run that uses real provider keys (it can rate-limit every model).
5. All quality gates are **alerts only**. No gate blocks a merge or a release automatically.

---

## 1. Purpose, users and scope

- **Product:** a question-answering assistant over a company's BFSI (banking, financial services, insurance) policy and regulatory documents. Every answer is grounded in the documents and cites clauses.
- **Users:** two audiences with different access.
  - **Customers** (external) — self-register; see only customer-accessible documents.
  - **Staff** (internal) — roles are assigned only by an admin; see staff-accessible documents.
  - **Admin** — manages documents, entities, users, evaluations and the review queue.
- **Autonomy:** read-only answers. The system never takes actions on behalf of a user and never calls tools chosen by a model.
- **Data:** real company policy documents. **No customer data** is ingested.
- **Language:** English only.
- **Hosting:** cloud API keys, any region, free tiers during the pilot.
- **Success KPIs:** groundedness, context relevance, answer relevance, context precision, context recall, low latency.

## 2. Technology stack and environments

| Concern | Choice |
|---|---|
| Backend API | FastAPI (Python) |
| Frontend | Streamlit (separate process/service from the API) |
| Orchestration | LangGraph |
| LLM access | LiteLLM used as a library inside a custom gateway (no LiteLLM Router) |
| Answer models | Groq `gpt-oss-120b` → Gemini `gemini-3.8-flash` → OpenRouter `google/gemma-4-31b-it:free` (mixed models are acceptable for the pilot) |
| Decision models ("decide tier") | **Jev** (`typesafe/jev-1.13`, through the OpenRouter API, own dedicated key) → Groq `gpt-oss-20b` → Gemini `gemini-3.8-flash` → rules (full chain in 5.1) |
| Embeddings | Google Gemini `gemini-embedding-2`, 768 dimensions, two API keys from different accounts with failover |
| Reranking | Cohere → Jina → lexical fallback |
| Vector store | Qdrant cloud (dense + sparse vectors in one collection) |
| Relational database | Cloud-hosted Postgres (Neon or Supabase), reached through a plain standard connection URL with TLS required; no provider-specific features. Migrations use the provider's direct (non-pooled) URL. No Postgres runs on the admin machine. Holds users, sessions, messages, audit, allow-list, feedback |
| Cache | Upstash (Redis protocol, `rediss://`) |
| PDF parsing | Unstructured.io (local, with poppler and tesseract) |
| Image understanding | Gemini hosted API |
| PII detection | Presidio |
| Tracing | Langfuse cloud |
| Evaluation | RAGAS (RAG metrics), DeepEval (edge-case suite), plus a light custom scorer for live production |
| Hosting | Render, free tier, two services (API and Streamlit) |
| CI/CD | GitHub Actions [P] |
| Migrations | Numbered SQL files |

**Two environments**
- **Local (admin machine):** ingestion, entity extraction, evaluation runs, load tests. Uses the large spaCy model (`en_core_web_lg`) for entity extraction.
- **Production (Render):** serves queries only. Uses the small spaCy model (`en_core_web_sm`) for query masking. **No ingestion runs in production.**

**Dependency files (four, separate)**
1. `requirements.txt` — runtime only (small spaCy model; no Unstructured, no tesseract, no poppler).
2. `requirements-ingest.txt` — large spaCy model, Unstructured, notes for installing poppler and tesseract.
3. `requirements-eval.txt` — RAGAS, DeepEval.
4. `requirements-dev.txt` — pytest, pytest-mock, pytest-cov, vcrpy, locust, and the PDF generator library.

**API keys**
- Up to four keys per provider (Groq, Gemini, OpenRouter) from different accounts, supplied as a comma-separated environment variable per provider [P: variable names such as `GROQ_API_KEYS`].
- **Separate eval keys** on top of the production keys. Eval keys are never installed on Render.
- Jev has its own dedicated OpenRouter key.
- Two Google embedding keys.
- The owner must check each provider's terms of service for using several accounts [T].

## 3. Architecture overview

```
 Streamlit UI ──► FastAPI ──► LangGraph pipeline ──► response
                                │
        ┌───────────────┬───────┴─────────┬──────────────┬───────────────┐
        ▼               ▼                 ▼              ▼               ▼
   LLM gateway      Qdrant           Upstash         Postgres        Langfuse
 (key pools,     (documents,      (answer cache)   (users, chats,   (traces)
  Jev, models)    later cache)                      audit, review)
```

Admin tools run on the admin machine: ingestion, entity review, evaluation, load tests, and the dashboards (admin-only tabs).

## 4. The query pipeline (LangGraph)

One shared state object travels through the graph (query, masked query, role, session, history, decisions, candidates, answer, flags, token counts, timings). Each stage is a node with its own timeout and a structured error. **Total request budget: 30 seconds [T: recalibrate after load tests]**; every node checks the time remaining.

### 4.1 Stage order

1. **Authenticate and rate-limit** (section 13).
2. **Local scan — no LLM, no network.** In this order:
   1. Query limits: **maximum 100 tokens** (counted with a real tokenizer). Over the limit → refuse with "please shorten"; never truncate silently. **Minimum 10 characters [T: confirm whether this is a hard refusal or applies only to the first message of a session — see 4.3].**
   2. Greeting check (4.2).
   3. Injection patterns (regex).
   4. PII masking of the query copy that goes to any LLM (section 9.2).
3. **Jev triage — one combined call** on the masked query. Returns, each with a confidence: domain relevance, `product_line`, `clause_type`, needs-history, vague/borderline flag, injection score. Fallback chain: Jev → Groq `gpt-oss-20b` → Gemini → rules and keywords with no LLM (full chain in 5.1).
4. **Injection decision [P]:** regex plus the Jev score. Any strong detector blocks; mixed signals ask the user to reword. The detector that fired is logged.
5. **Rewrite** (answer-tier LLM) runs only if (a) needs-history is true, or (b) it is a vague first message of a session (rewrite-then-clarify, 4.3). Otherwise the original query is kept.
6. **Domain gate:** out-of-domain queries are refused.
7. **Cache lookup:** exact cache first (section 10); a hit is verified before returning.
8. **Embed** the (rewritten) query with embedding key A, then key B.
9. **Retrieve** from Qdrant (section 7): hybrid dense + sparse, 25 candidates.
10. **Rerank** Cohere → Jina → lexical; keep the **top 5** (fixed for now; a dynamic count may be tested later and adopted only if evaluation is better).
11. **Context guard:** drop chunks below the relevance floor [T: starting value 0.30, tune from evaluation]; scan chunk text for instruction-like content.
12. **Low-confidence branch:** if confidence is low, the answer states its confidence, says it may be incorrect, and asks for a clearer query. Refused and low-confidence answers are also flagged to the review queue (section 11.4).
13. **Generate** (answer tier: Groq → Gemini → OpenRouter). The prompt contains static instructions first, then the labelled context chunks, then the question. **The generator never sees session history.**
14. **Output guard:** structured schema check, numeric check, citation check, lexical groundedness (section 9.4).
15. **Cache write** only if the answer is grounded, verified, has no warning, and the corpus version did not change during the request.
16. **Persist:** save the masked messages, update the summary buffer and session facts, write the trace, write audit rows where applicable.
17. **Respond.**

### 4.2 Greetings
If the **whole** message is only a greeting or farewell (hi, hey, hello, good morning, bye, goodbye, thanks, thank you), return a fixed deterministic reply: no LLM, no retrieval, no cache. A message that contains a real question as well ("hi, what is KYC?") goes through the normal pipeline.

### 4.3 Vague first message
A first message of a session that is vague gets rewrite-then-clarify: the rewrite step restates it using a **static BFSI domain checklist** hard-coded in the prompt; if it is still unclear, the assistant asks the user for more detail and shows what it understood. Follow-up messages have no minimum length. **Two unclear turns in a row** end with a fixed message and a flag in the review queue.

### 4.4 New session versus existing session
- **New session:** no history; vague handling per 4.3.
- **Existing session:** load the summary, session facts and the verbatim buffer (section 8); the history-based rewrite resolves follow-ups such as "and for senior citizens?".

## 5. LLM gateway

A single module wraps every model call. No other code calls a provider directly. The gateway module covers LLM, embedding and rerank calls, using the same key pools, limiter and breaker for all three.

### 5.1 Tiers and fallback
- **Answer tier:** Groq → Gemini → OpenRouter.
- **Decide tier:** Jev → Groq `gpt-oss-20b` → Gemini → OpenRouter → answer-tier models → rules. **Every Jev use has a fallback.** Jev is called through OpenRouter's Decisions API (alpha), POST /api/alpha/decisions, with a state object and typed questions (noul, choice, score); it returns probabilities, not text. The fallback deciders answer the same typed questions with JSON of the same shape.
- Evaluation verdicts: Jev → OpenRouter judge model (eval keys) → "not scored".
- Offline mock generation exists only as a last-resort fallback and is always labelled as such.

### 5.2 Reliability features
- LiteLLM typed errors (rate limit, timeout, bad request, context-window exceeded, auth).
- Exponential backoff with jitter (base 0.5 s, max 4 s, up to 3 attempts per provider) [P: values].
- A sliding-window **requests-per-minute and tokens-per-minute limiter per key** [T: configured values — Groq 30 RPM / 8,000 TPM, Gemini 10 RPM / 250,000 TPM, OpenRouter 20 RPM; verify against each provider console].
- **Key pools:** each provider has up to four keys; calls rotate across keys in turn. A 429 puts only that key on cool-down and the call moves to the next key. The provider-level circuit breaker trips only when **all** keys are limited or failing. Provider fallback happens only after the pool is exhausted.
- A circuit breaker per provider and tier.
- Oversized prompts are not retried.
- Structured outputs through `response_format` where the model supports it; otherwise JSON mode with tolerant parsing.
- Max output tokens: 400 for answers, 500 for JSON calls [P]. Check that hidden reasoning tokens of the open models do not truncate answers.
- **[P] Additions beyond the text above:** a per-provider billing-plan setting (free or paid) that decides whether the billed cost is shown as $0 while the shadow cost stays the list price; optional per-key daily request and token caps; a default call timeout of 10 s (the caller's budget can only shorten it); and fixed cool-down and breaker times (60 s for a rate-limited key, 30 s for an open breaker).
- **[P] Further constants, not in the text above:** a 3,600 s cool-down for a key that fails authentication or has no credit left; 30 s cool-downs for a key that timed out or returned a 5xx (and for a failed embedding key); a 5 s minimum cool-down when a provider sends a shorter `Retry-After`; a 32,000-token limit for Jev; and the per-key daily caps, which are on by default (for example OpenRouter 50 requests per day per key, Groq 1,000 requests and 200,000 tokens).
- **[P] Production guards:** the mock generator and the eval keys are refused when `RENDER` is set.
- **[P] Reasoning models:** an answer that comes back empty because the model spent its output budget thinking is never returned; it is retried once with more output room and reasoning effort low, then falls to the next provider. The reserved output is the answer limit plus a reasoning allowance (default 512 tokens), and the default reasoning effort is low for answers and decisions; all three are environment settings.

### 5.3 Decider visibility
Every decision records: `decider` (jev, groq, gemini, rules), model, key fingerprint, fallback reason. The dashboard has a **"decider in use"** panel per step with the current health (healthy, cooling down, circuit open) and the share of decisions per decider over the last hour and day.

### 5.4 Cost and tokens
- LiteLLM cost per call, with a `cost_source` label.
- **Shadow cost:** free models report $0, so the dashboard also shows a shadow cost computed from a reference price table (the paid equivalent of the model, or Groq `gpt-oss-120b` pricing if none exists) [P]. Billed cost and shadow cost are always labelled separately.
- Input and output tokens are counted separately with a real tokenizer; output is reserved at `max_tokens` before a call and corrected afterwards.
- Dashboard: p50, p95, p99 tokens per request, cost per answer, cost split input/output, tokens per stage.
- **Per-user daily token budget:** customers start at 50,000 tokens; staff higher [T]; to be calibrated after the baseline. An over-budget request is refused with a clear message before reaching a model.
- **[T]** The staff token budget is a placeholder of 200,000 tokens a day (admin the same). The day is the UTC day and resets at 00:00 UTC.
- Capacity note: with Groq at 8,000 tokens per minute, one answer costs about 3,400 tokens, so one Groq key serves about 2 answers a minute; key pools multiply this.
- LiteLLM caching is **not** used.

### 5.5 Context windows
Read each model's window from LiteLLM's model map at startup, with a configuration override (some free OpenRouter models have lower real limits). The prompt is built once and reused on fallback, so the check uses the smallest window among the planned providers. Typical budget per generation call: about 25 tokens of instructions, 1,750–3,000 of chunks, up to 100 of query, 400 reserved output.

## 6. Ingestion (admin, local machine only)

### 6.1 Parsing
- Unstructured.io partitions each PDF into typed elements (titles, narrative text, list items, tables, images) with page numbers.
- **Images:** the image bytes are sent to the Gemini hosted API, which describes them as text; the text is embedded as a chunk. Because document content leaves the machine, this must never run on documents that contain customer data.
- **[P]** The gateway has one image call, `caption_image` (PNG, JPEG or WebP up to 8 MB): Gemini only, on the same key pool, rate limiter and circuit breaker as Gemini answers; neither the image bytes nor the caption text is ever logged or put in an event. A caption chunk is identified by the image's hash, not by the caption, so an unchanged picture is never captioned twice. For now only the sample documents are ingested; a customer-specific document sends nothing to any model (no captions, summaries, labels or entity questions) until a paid or zero-retention plan is in place.
- **Tables** are kept as tables.

### 6.2 Chunking [P]
- Numbered clauses ("Section 3.", "Clause 2.1", "3.2") split on the clause number; unnumbered text is grouped by Unstructured under headings.
- Target about 450 tokens, hard maximum about 600, overlap about 60 tokens only when an oversized clause is split; splits happen at sentence boundaries, with the clause heading repeated in each piece.
- A table is one chunk if it fits; otherwise it is split by rows with the header row repeated. **Every table chunk gets a one-line summary** (decide-tier call).
- Image-description chunks are about 100–250 tokens, prefixed with page and heading context.
- Fragments under about 50 tokens merge with a neighbour under the same heading.
- Each chunk gets a clause label from the decide tier (exclusion, waiting period, KYC, claims, portability, copayment, grievance, general).
- Chunk sizes are starting points; the baseline evaluation (context precision and recall) confirms or changes them.

### 6.3 Identity and incremental reindexing
- A chunk's identity depends on **content and clause number, never position**. A stable `clause_ref` is taken from the document's own numbering (for example "3.2"). Chunks without a clause number use the content hash plus the nearest heading.
- Qdrant point id = hash of `doc_id + clause_ref + part_index`.
- On re-ingest, chunks are matched by `content_hash` first, then paired by `clause_ref`. Only new or changed chunks are re-embedded. Inserting a clause in the middle of a document must not change the identity, version or embedding of any other chunk.
- A replaced or removed chunk stops being returned **immediately** (`effective_to` is set).
- Whitespace-only changes are not changes (hashes are computed on stripped text).
- Page numbers are stored for citation display only and are never part of identity.
- A per-document lock prevents concurrent ingest, reindex and delete on the same document; ingest and delete are safe to repeat.
- If one page fails to parse or caption, the document is **flagged for the admin** and the page is retried later; no half-indexed documents.
- **[P]** A document with a failed page or caption is **not activated**: nothing is written, the previous version (if any) stays active, each failed page gets one open `failed_ingest_page` row in the review queue (page number and a short reason, no page text), and running the same ingest again retries it and closes the rows once every page succeeds. An empty parse never deactivates a stored document.
- **[P]** Re-ingest matches new chunks to stored ones by content hash first: an unchanged chunk, even if moved or renumbered, keeps its point id, vector and version, and only its payload (clause, heading, pages, role) is refreshed. Chunks are then paired by identity key: the same clause keeps its point id and gets `chunk_version` + 1. The rest are new or removed. A clause number that repeats in one document gets a numeric suffix on its identity key. A fragment under about 50 tokens merges only into another unnumbered chunk under the same parent heading, so adding or removing a numbered clause never changes another clause's chunk. All embedding finishes before the first write; the corpus version is bumped after the writes, and cache entries are cleared after the bump. A bump or an invalidation that fails makes the command exit non-zero and say "ingest incomplete" (running it again finishes the job).

### 6.4 Payload fields stored with each vector [P]
`doc_id`, `source`, `chunk_id`, `clause_ref`, `part_index`, `content_hash`, `doc_version`, `chunk_version`, `effective_date`, `effective_to`, `page_start`, `page_end`, `heading_path`, `clause_type`, `product_line`, `content_type` (text, table, image_caption), `image_ref`, `access_role`, `ingested_at`, `parser`.

**[P]** Four more fields are stored: `text` (the chunk text, which retrieval returns), `summary` (a table chunk's one-line summary, embedded but not part of the content hash, so a new summary never makes a chunk "changed"), and `quarantine` and `quarantine_reason` (9.3). `doc_version` on a chunk is the document version at which that chunk's current content was written, so adding a clause leaves every other chunk's version untouched. Each ingest command takes a required stable `--doc-id` slug (never derived from a path), a required `--doc-type` (policy, regulatory, customer-specific) and `--access-role`; `product_line` is an optional argument.

### 6.5 Embeddings
- Gemini embedding with two keys from different accounts; key B is used when key A fails (same model, same 768 dimensions, so vectors are interchangeable).
- **[P]** The Gemini Embedding 2 query and document text prefixes (`task: search result | query: ...` and `title: ... | text: ...`) come from Google's documentation. Using `title: none` for untitled documents is an assumption; the owner confirmed in Phase 5 that the document title is used when there is one and the literal `none` otherwise.
- If both fail: queries get "temporarily unavailable"; ingestion stops and flags the document. **A hash-based fallback embedding is for development only** and must never index or retrieve in production.

### 6.6 Sparse vectors
Built with the project's own small tokenizer (lowercase, strip punctuation, light stemming); Qdrant applies IDF. No fastembed.

### 6.7 Delete
An admin **Delete** button sits next to the ingestion button. After a confirmation step it removes all vectors of the document at once, removes the document's allow-list entries, clears related cache entries (chunk and document reverse sets), and writes one audit row (document id, who, when, chunk count). No document text is kept in the audit row. Existing transcripts that cited the document are flagged "source removed" (8.5).

### 6.8 Entity allow-list extraction (at ingestion, local)
Purpose: do not mask legitimate terms (laws, regulators, places, products, public helplines, named officials quoted in a policy) while still masking everything that came from the user.
1. Candidates come from NER (large spaCy model with Presidio), pattern extractors (acts and regulations, helplines, emails, URLs, IFSC, CIN, product codes), curated seed lists (regulators such as IRDAI, SEBI, RBI, NPCI, the company name), and document structure (product names from titles and headings; names next to a designation).
2. Deterministic variant generation ("Rahul Sharma" → "Mr. Sharma", "R. Sharma", "Sharma").
3. In policy and regulatory documents, laws, regulators, organisations, places and helplines are **auto-approved**. Customer-specific documents have their candidates rejected automatically.
4. People and uncertain candidates go to a decide-tier classification with surrounding text; confident cases are approved; the rest become **pending**.
5. The admin reviews pending people in bulk, seeing each name with its context snippet, and approves name variants as a group. **Single-word variants are approved only when the reviewer ticks them.**
6. Matching is **exact on normalised variants** — no fuzzy matching.
7. Entries are tied to `doc_id`, removed when the document is deleted, and only changed chunks are re-extracted.
8. **[P]** One allow-list row is stored per variant. Pattern kinds that are not in the auto-approved list (people, product codes, IFSC and CIN) are put to a decide-tier question with the surrounding text; an answer of at least 0.85 is approved, anything else (or no answer) stays pending. In a customer-specific document every candidate is rejected and no model is called. The product names come from the document title and from headings that end in a product word (plan, policy, scheme, fund and so on). A term already stored for the document is not stored twice and a reviewed status is never overwritten. The company name seeds come from the `COMPANY_NAMES` setting [T]. The review command groups a person's variants, shows the sentence each name appears in, and approves the group with one answer; the surname alone is approved only when ticked. Matching later uses `normalise_entity` (NFKC, case-folded, whitespace collapsed, quotes and commas trimmed), exact and not fuzzy.

## 7. Retrieval

- **One Qdrant Query API call** runs the dense search and the sparse search as prefetch branches and fuses them with Reciprocal Rank Fusion (k = 60). The `access_role` and `effective_to` filters are repeated on **both** branches.
- **Hard filters (always):** `access_role` from the logged-in user; only active chunks (`effective_to` empty).
- **Soft filters:** `product_line` and `clause_type` come from the Jev triage call as closed choices with confidence. The allowed values are the distinct values stored in Qdrant, refreshed when the corpus version changes. A soft filter is applied only when its confidence is high; otherwise it is only a score boost. If the filtered search returns nothing above the relevance floor, retry once without the soft filters.
- **Neighbour context:** each retrieved chunk may carry its heading path and the adjacent clause.
- 25 candidates are retrieved; reranking keeps 5.
- Every cited chunk returns `clause_ref`, `heading_path` and page numbers for citations.
- The `access_role` isolation must hold on every path (tested per role).

## 8. Memory and sessions

### 8.1 Scope
**Short-term memory only.** No cross-session long-term memory in this version (the mem0 library may be evaluated later).

### 8.2 Summary Buffer
- The rewrite step receives a running **summary** plus a **verbatim buffer** of recent turns.
- Starting values: buffer 400 tokens, summary up to 150 tokens, about 550 tokens of history in the rewrite call; each past assistant answer clipped to about 80 tokens. **At build time, sweep the verbatim buffer at 200, 400, 800 and 1,200 tokens** on the multi-turn evaluation set and choose the smallest size where rewrite accuracy stops improving.
- When the buffer overflows, the oldest turns are folded into the summary by a decide-tier call (one call per overflow).
- The summary is built **only from masked text**.
- The **generator never sees history**.

### 8.3 Persistence and resume
- Sessions and messages persist in Postgres so a user who returns to a session can continue it.
- New columns on sessions: `summary_text`, `summary_upto_message_id`, `session_facts` (JSON), `last_active_at`, `status` [P].
- On resume: load the summary and session facts, then rebuild the verbatim buffer from the messages after `summary_upto_message_id`, within the token limit. The "last 3 turns" are derived from stored messages, not stored twice.
- `session_facts` is structured state (current product line, topic, approved entities) filled from the Jev triage output and deterministic entity matches — **no extra LLM call** [P].

### 8.4 Retention and deletion
- Chats are kept **90 days**. Users can delete their own chats themselves.
- When a document is deleted, any summary built from it is cleared.

### 8.5 Source removed
Transcript messages that cite a deleted document are flagged **"source removed"** in the history view. **[P]** `messages.citations` is a JSON list of objects, each with the `doc_id` of the cited document (and its clause and page); delete flags every message with any citation whose `doc_id` is the deleted document.

### 8.6 Protection against context failures
| Failure | Mitigation |
|---|---|
| Poisoning (an error is reused) | Only grounded, verified answers feed history and summary (the `grounded` flag); refusals and ungrounded answers are marked, not repeated; the summary is built from user questions plus cited facts, not free model prose; the generator never sees history; only verified answers are cached |
| Distraction (context too long) | History token cap, 5 chunks, summary instead of raw history |
| Confusion (irrelevant content) | Soft metadata filters, rerank, relevance floor; optional sentence pruning later |
| Clash (contradictions) | Only one active version per clause; each chunk labelled with source and effective date in the prompt; the model is told to flag conflicts; the numeric check helps detect them; the summary is never given to the generator |
| Isolation | Each LLM task (rewrite, triage, summary, judge) runs in its own small context |

## 9. Guardrails

### 9.1 Principles
Cheap, local checks run first. Guardrail time budget: well under about 300 ms in total [P]. Where a rule can be code instead of a prompt instruction, it is code.

### 9.2 PII masking
- Presidio plus regex for structured identifiers (policy numbers, IDs, phones, emails) plus a name-introduction pattern.
- Masking happens **locally before any LLM call** on the query copy sent to models.
- The approved-entity allow-list (6.8) is passed to masking so approved terms are not masked. At answer time, only approved entries whose `doc_id` is among the surviving chunks are unmasked [P].
- Place names (`LOCATION`) are not masked [P].
- A name typed by a user that is in no approved document stays masked in answers.
- Production uses the small spaCy model.
- Traces, logs, caches and stored transcripts contain only masked text.

### 9.3 Injection defence
- Regex patterns plus the Jev injection score; no separate injection model for now.
- **Indirect injection:** retrieved text is wrapped in clear delimiters and the model is told to treat it as data; chunks are scanned for instruction-like content at ingestion and again in the context guard.
- **[P]** A chunk that matches the narrow ingestion patterns (for example "ignore the previous instructions", "reveal your system prompt", a chat-template marker) is stored but **not active** (`effective_to` is set and `quarantine` is `pending`) until the owner approves it with the review command. An approved chunk becomes active and counts as a new chunk (corpus version bump, cache entries of the document cleared). A rejected chunk stays inactive and is not recreated by a later run of the same text.

### 9.4 Output checks
- The answer must match the structured schema (answer text plus citations).
- **Numeric check:** every number, percentage, date and amount in the answer must appear in the cited chunks.
- **Citation check:** every cited chunk id must exist in the retrieved set.
- Lexical groundedness check.
- **When the numeric or citation check fails: return the answer with a warning**, highlighting the unverified numbers.
- A judge-model groundedness check runs in evaluation and on sampled live traffic, not on every answer.

### 9.5 Policy
- No personalised financial, investment or legal advice. A standard disclaimer is shown **only when relevant** (advice-like or financial-figure answers).
- Out-of-domain questions are refused.

### 9.6 Prompts
Prompts are **versioned files** in a `prompts/` folder with a fixed structure: role, task, constraints, output format, one good and one bad example, and an explicit refusal rule. The hash of each prompt is recorded in every trace. Prompts stay short; rules move into code. Prompt token length is tracked over time.

## 10. Caching

### 10.1 What exists
- **Exact answer cache (first, build now).** Key = hash of the normalised, masked query plus role, corpus version and prompt version. One fetch, no scanning.
- **Semantic cache (later, only if the exact cache's measured hit rate justifies it).** Stored in a separate Qdrant collection (`answer_cache`) filtered by role and corpus version, never in the document collection. A semantic hit also requires the same entity and number signature as the query, to avoid wrong hits ("diabetes" versus "hypertension").
- Optional small exact caches for query embeddings (masked query plus embedding model) and for Jev triage results (tied to corpus version) [P].
- Provider prompt caching is not relied upon; static instructions go first in every prompt, and the cached-token count reported by each provider is recorded on the dashboard.

### 10.2 Rules
- Only grounded, verified answers with no warning or low-confidence flag are cached. Refusals and errors are never cached.
- Cache entries store no raw query text and no PII.
- Cache is scoped by role.
- TTL is only a backstop.
- **[P]** After an Upstash failure the cache is skipped for 30 s, so a dead cache costs one timeout and not one per call.

### 10.3 Staleness (immediate, independent of TTL)
1. A **corpus version** counter is part of every key; any ingest, edit or delete bumps it.
2. Each entry records the `chunk_id` and `content_hash` of every chunk it cited, plus their `doc_id`s; reverse sets `chunk:{id}` and `doc:{id}` list the entries.
3. **Edited or removed chunk:** invalidate entries citing that chunk. **Unchanged chunk** (even if moved): keep. **New chunk:** invalidate entries that cited the same document. **Whitespace-only change:** nothing.
4. **Read-time check:** on a hit, fetch the cited chunks' payloads and compare content hashes and `effective_to`; discard on any mismatch.
5. A request that began before an ingest must not write to the cache if the corpus version changed meanwhile.
6. The sparse index is refreshed in the ingest and delete path.
7. Role or permission changes bump the version for that role scope.
- **[P]** The corpus version starts from the current Unix time in milliseconds (never from 0), so a counter that is lost restarts higher than any earlier value. The bump and the invalidations are never skipped when Upstash had a recent failure, and a failure of either is an alert, because ingest and delete are not complete without them.

## 11. Reliability

### 11.1 Failure behaviour
| Dependency down | Behaviour |
|---|---|
| Qdrant | "Temporarily unavailable" (no ungrounded general answer) |
| Both embedding keys | "Temporarily unavailable" |
| Upstash cache | Skip the cache; answer normally |
| Postgres | Answer if possible and queue the transcript write [P] |
| One provider or all keys of one provider | Next provider in the chain |
| All providers | "Temporarily unavailable"; alert |
| Jev | Fallback chain in 5.1 |
| Rerank | Next reranker, then lexical; the UI shows a degraded-mode notice |

When any fallback is active (mock answer, skipped rerank), the UI says so instead of presenting the answer as normal.

- **[P]** A store that answers but refuses us (a rejected key or password, an unknown role, database or tenant, a missing server access rule, a TLS certificate failure) is reported to the operator as a settings error, not as "temporarily unavailable"; only a store that cannot be reached counts as down.

### 11.2 Endpoints
- Full **health endpoint: admin-only**.
- A minimal **public liveness endpoint** that reveals nothing, for the uptime pinger.

### 11.3 Backups
Periodic Qdrant snapshot, or at minimum the ability to re-ingest from the original PDFs, which are kept in storage [P].

**[P]** The original PDFs are kept on the admin machine in a git-ignored folder `originals/<doc_id>/<sha256>.pdf`, copied once and never overwritten. It is the only copy the project keeps, so the owner backs it up. The local model-answer cache (`originals/.cache/`) holds only text derived from documents and may be deleted at any time.

### 11.4 Human-review queue
Refused answers, low-confidence answers, thumbs-down answers, two-unclear-turn sessions and failed ingest pages land in an **admin review queue**. From it the admin can label an item and add it to the evaluation set with one click. The admin reviews the queue; the overdue limit is [T: placeholder 2 working days].

## 12. Data model

### 12.1 Postgres (main database) [P for field details]
- `users`: id, username, email, password hash, role (`customer`, `staff`, `admin`), created_at.
- `revoked_tokens`.
- `sessions`: id, user id, title, created_at, plus the columns in 8.3.
- `messages`: id, session id, role, content (masked), citations, grounded flag, source-removed flag, created_at.
- `audit_log` (append-only): who, what, when, target, details. Every ingest, reindex, delete, role change, login, and evaluation run writes a row. **Retention 30 days for the pilot.**
- `allowlist_entities`: id, doc_id, text, entity_type, status (pending, approved, rejected), approved_by, approved_at, access_role.
- `feedback_entries`; `review_queue`.
- **[P]** `users` also has `is_active` (default true): a deactivated account cannot sign in and loses access at once.
- Migrations are **numbered SQL files** applied in order by a small runner; each migration is tested against a copy of the data.

### 12.2 Evaluation database (separate)
A different database from the main one: `eval_runs` (run id, environment `local` or `production`, dataset version, system version [prompt hashes, model ids, corpus version], timestamps), `eval_results` (item, metric, score, decider), `eval_cases`, production samples, drift snapshots. The local environment writes to a local SQLite file; production writes to its own separate cloud Postgres database (free tier, Neon or Supabase). [T: confirm at the evaluation phase]

### 12.3 Qdrant
- `documents`: named dense vector (768) and sparse vector; payload in 6.4; payload indexes on filterable fields.
- `answer_cache` (later, 10.1).

### 12.4 Upstash
Exact-cache keys, reverse sets, rate-limit counters.

## 13. Security and compliance

### 13.1 Accounts
- Customers self-register and always receive the customer role; **staff roles are assigned only by an admin**; registration can never set a role.
- JWT login with expiry and token revocation; login throttling; a password rule [P].
- Admin MFA: later.
- **[P]** Password rule: 10 to 128 characters, counted after Unicode NFKC normalisation (which is also applied before hashing); no forced character classes; must not contain the username or the email name; must not be on a short built-in list of very common passwords. Passwords are hashed with argon2id (19 MiB memory, 2 passes).
- **[P]** Login tokens last 8 hours and hold only the user id, a token id and the expiry; there are no refresh tokens. Every request checks the token against the revoked list and against the database: the account must exist and be active, and its role is read from the database, so a role change, a deactivation or a logout takes effect at once.
- **[T]** Limits: 10 login attempts per username and 30 per client address in 15 minutes; 5 registrations per client address per hour; 20 requests a minute per signed-in user. The client address is the socket address; a proxy header is trusted only after the host's trusted proxy is configured. If Upstash is down, login and registration throttling continue in the process instead of switching off.
- **[P]** Two separate secrets, `JWT_SECRET_KEY` (signs tokens) and `AUDIT_HASH_KEY` (keys the hashes of usernames in audit rows and throttle counters), each at least 32 random bytes and unrelated to each other (not equal, not one inside the other, not reversed, not one with a few characters changed); the API refuses to start on an empty, short, placeholder, repeated-pattern or keyboard-run value (a 64-character hex key is accepted). Each login that is checked and each role change writes an audit row; a username that is not an account is stored only as a keyed hash. A login refused by the throttle writes no audit row: it is counted and raises an alert event instead (for Phase 12), and it checks no password.
- **[P]** Only the owner-run command creates admins. The API can make an account a customer or staff, and never changes an account that is already an admin. The last active admin can never be deactivated.
- **[P]** At most 2 password hashes run at the same time (`PASSWORD_HASH_CONCURRENCY`); a request that finds every slot taken is answered 503 at once, so a burst cannot use up the host's memory.
- **[P]** Builder choices not stated above: usernames are 3 to 32 lower-case letters, digits, dots, dashes or underscores; an email must pass a simple check (at most 254 characters); a token also carries its issue time; a password hash is upgraded at login when the settings get stronger; there is a `/auth/me` endpoint, an owner command to deactivate an account, and a `Cross-Origin-Resource-Policy` header; the in-process throttle holds at most 10,000 entries; a token lasts at most 30 days whatever the setting.

### 13.2 OWASP LLM Top 10 mapping
| Risk | Control |
|---|---|
| Prompt injection | Local scan, regex plus Jev score, delimiters, ingestion scan |
| Sensitive information disclosure | Mask before any LLM call; no PII in caches, traces, logs; scans |
| Supply chain | Pinned versions, no unused packages, `pip-audit` in CI |
| Data and model poisoning | Admin-only local ingestion, entity review |
| Improper output handling | Render model text as plain text; strip HTML and links |
| Excessive agency | None — read-only |
| System prompt leakage | No secrets in prompts |
| Vector and embedding weaknesses | Role filter on both search branches, tested per role |
| Misinformation | Groundedness, numeric and citation checks, confidence warning |
| Unbounded consumption | 100-token cap, token budgets, rate limits |

### 13.3 Hardening
CORS restricted to the frontend address; security headers; request size limit; secrets only in the host's secret store; a secret scanner (gitleaks) and `pip-audit` in CI; key rotation routine.
- **[T]** Request size limit: 16 KiB.
- **[P]** Every response carries the security headers (nosniff, no framing, no referrer, no store, a Content-Security-Policy of `default-src 'none'`, a permissions policy and HSTS). CORS allows nothing unless the one frontend origin is configured, and never with cookies. The interactive API docs and the API description are not published.

### 13.4 Data-flow map
Maintain one page listing what each vendor sees: Groq, Gemini, OpenRouter and Jev (masked query, retrieved chunks), Cohere and Jina (retrieved chunks and the masked query), Langfuse (masked traces), Upstash (cached answers), the Postgres host, Neon or Supabase (masked transcripts), Qdrant (document vectors).

### 13.5 Accepted risks for the pilot
Free-tier data terms and any-region hosting are accepted, under the rule: **no real customer data until paid or zero-retention plans are in place.**

### 13.6 Data subject requests
A user can delete their own chats; related traces are removed by session id.

### 13.7 Regulatory frame
The DPDP Act 2023 applies to personal data; RBI, IRDAI and SEBI rules depend on the company's licences. Confirm with a compliance advisor [T].

## 14. Observability

- **Langfuse cloud** with typed observations per stage, session and user ids, tags, masked input and output.
- Audit-grade fields on every trace: prompt hash, corpus version, retrieved chunk ids with content hashes and `clause_ref`, filters applied, decider per decision, key fingerprint, model, input and output tokens, cost and shadow cost, guardrail verdicts, cache status (hit, verified, discarded), confidence shown.
- Structured JSON logs carrying a correlation id; the id is recorded internally but **not shown to users**.
- Separate append-only `audit_log` (12.1).
- A scan confirms traces and logs contain no PII or secrets.
- **Alerts:** a chat webhook plus a dashboard banner (no email). Alerts include: rising error or fallback rate, all keys exhausted, drift threshold crossed, Jev outage, guardrail spike, budget variance.
- **Uptime:** an external pinger on the liveness endpoint, which also keeps the free host from sleeping (it consumes free hours).
- **Dev-only live stage printer (terminal only):** controlled by a `DEV_STAGE_TRACE` flag, **off by default and off in production**. While a query runs it prints each stage as it starts and finishes with its time, the decider/model/provider and key fingerprint used, token counts, and a final waterfall with each stage's share, the slowest stage and unaccounted time. It prints stage names, timings and counts only — never query text. No "slow" marking. Langfuse stage timings stay on in production.

## 15. Evaluation

### 15.1 Principles
- Evaluation covers the **whole system**, not just retrieval.
- The judge is an **OpenRouter model that differs from the generator**, including after fallbacks.
- Never change the evaluation set and the system in the same week.
- Evaluation runs on a separate test Qdrant collection and a separate database, never on live data.

### 15.2 Evaluation set
- **60 labelled cases** covering: factual, table lookup, image-derived, multi-turn, vague, greeting, out-of-domain, injection, PII, role isolation, edit and delete, conflicting versions, source removed.
- **20 are active** in normal runs (a stratified core with at least one per category); the other 40 run before releases and on demand.
- Expected sources are stored by `clause_ref`, not chunk id.
- Calibration: **30 items** are hand-labelled to measure judge agreement. The project's AI assistant labels them; expert review of a sample is recommended [T].
- A thumbs-down or refused answer can be added to the set from the review queue.

### 15.3 Frameworks
- **RAGAS** for RAG metrics; **DeepEval** for the edge-case suite and custom checks (such as numeric faithfulness); a **light custom scorer** for live production (cheap checks and Jev verdicts), with full RAGAS scoring in local batches.
- Their model calls go through the gateway via a custom LLM wrapper (key pools, limiter, Jev).
- **Jev is used in evaluations where possible** for classification-type steps (is this chunk relevant, is this claim supported, does the citation support the claim, was the refusal correct, was the filter or injection call right). The judge LLM handles steps that write text (claim extraction, answer-relevancy questions, nuanced correctness). Jev is used for a metric only after calibration shows high enough agreement with the judge and the labels.
- If both Jev and the judge are unavailable, the metric is recorded as **"not scored"**.

### 15.4 Runs
- **Local:** an admin-started "Run eval" button (local environment only) shows an estimated token cost and duration first, checkpoints each item, resumes after a stall, and updates the dashboard live. It uses the separate eval keys.
- **Production:** about **5% of live answers** are queued for scoring off the request path; a rate-limited judge waits in the queue. A weekly drift report.
- **Variance band:** run the active set three times unchanged; the band is about two standard deviations. Inside the band is noise.

### 15.4a Metric inventory
- **Floor:** faithfulness/groundedness, context recall, answer relevancy, latency p95, cost per query, golden-set score against the variance band, guardrail trigger accuracy, audit log completeness.
- **RAG:** context precision, context recall, context relevance, context entity recall, faithfulness, answer relevancy, answer correctness, answer similarity (optional), hallucination rate (reference-free), citation accuracy, index freshness lag, latency p95 for retrieval and generation, cost per query, "answered correctly" rate, numeric faithfulness, refusal correctness, PII leak rate.
- **Gateway:** gateway-added latency p95, cache hit rate, failover trigger and success rate, rate-limit rejection rate, cost attribution completeness.
- **Orchestration and reliability:** per-node time and failure, branch frequencies, time-budget hits, retry rate, circuit-breaker trips and recovery, timeout rate per dependency, duplicate ingests (target 0), failure tax.
- **Retrieval:** hit rate, precision and recall at 5, reranker lift, sparse and dense contribution, soft-filter accuracy and fallback rate.
- **Memory:** summary fidelity, resume fidelity, staleness and contradiction rate, session isolation, history tokens p95, clarification-loop cap hits.
- **Guardrails:** guardrail latency p95, false positive and negative rates on PII and injection, numeric and citation failure rates, active prompt version and time since last regression pass.
- **Jev:** per-field accuracy, valid typed-output rate, latency p95, fallback rate, key health.
- **Ingestion:** parse success rate, tables intact, image captions created and failed, chunk size distribution, re-embed ratio on edits, entity extraction precision and recall, pending review count, ingest duration, image-token usage per document.
- **Caching:** hit rate (exact and semantic separately), wrong-hit rate (target 0), stale-answer incidents (target 0), commands per lookup (constant), cached share of input tokens.
- **Observability and operations:** trace coverage %, error correlation completeness, MTTD and MTTR, uptime, error rate, p95 and p99 latency, cost against budget, incidents, rollback frequency, vendor and model dependency health (deprecation notices).
- **Business:** see 15.6.

### 15.5 Eval dashboard (admin-only)
Two views, never mixed: **local (test)** and **production**. Panels: quality scores with the variance band shaded; per-category scores; stage latency waterfall; provider and model mix and fallback rate; tokens in/out and cost (billed and shadow); guardrail confusion matrix; cache hits and staleness; decider in use; key pool health; ingestion health; drift.

### 15.5a Drift
- **Model drift:** every category dips together with nothing changed on the project side. Track the share of answers and the score **by actual model** (mixed models make this the highest risk). Pin or re-baseline deliberately.
- **Prompt drift:** one category moves; the prompt hash in each trace identifies the change.
- **Retrieval drift:** groundedness and hit rate fall together while answer relevancy stays flat; check the corpus.
- **Data drift signals:** mix of product lines and clause types asked about; vague, greeting, refusal and out-of-domain rates; query length; distance of query embeddings from the baseline; share of queries whose best retrieval score is below the floor (coverage gaps); corpus changes and corpus version over time; embedding model or key changes; cost and latency creep.
- Method: compare weekly distributions against a baseline window with a divergence measure (population stability index or Jensen-Shannon) and the variance band for scores. Alerts only.

### 15.6 Business owners and metrics
Two owner roles. Names below are **placeholders** to be replaced with real people.

| | External owner — Meera Iyer, Head of Customer Experience (placeholder) | Internal owner — Arjun Nair, Head of Operations (placeholder) |
|---|---|---|
| Audience | Customers | Staff |
| Adoption | Usage frequency among active customers | Daily active staff over staff accounts (seat utilisation) |
| Deflection | Chats closed with a "Helpful" rating and no human hand-off | Time-to-resolution saved per lookup [T: needs a pilot baseline or an optional "time saved" prompt] |
| Business KPI | Customer satisfaction (thumbs now, 1–5 rating later) | Operational efficiency and compliance: audit completeness, answers with verified citations |

Telemetry and evaluation profiles are split by user role (customer versus staff). Daily customer and staff metrics start from launch.

## 16. Testing

- **Isolation guard:** tests refuse to run if the database, Qdrant or cache URL points at any remote host (cloud Postgres, Qdrant cloud, Upstash). Only SQLite, in-memory Qdrant and localhost are allowed. Unit tests use SQLite and Qdrant in-memory mode. Postgres-specific tests run only in CI, on a throw-away Postgres service container on the CI runner; they never run on the admin machine and are skipped when no test Postgres URL is set.
- **Unit tests:** gateway (circuit breaker, limiter, backoff, key-pool rotation, Jev-to-Groq fallback, typed errors); guardrails (PII, injection, greeting, length limits, numeric and citation checks, allow-list scoping); pipeline branches with stubbed gateway and retrieval (cache hit, refusal, vague, low confidence, normal, timeout); cache and reindex (staleness scenarios; inserting a clause mid-document re-embeds only that clause); memory (summary, resume, session isolation, 90-day retention, user self-delete); auth (registration cannot set a role, throttling, revoked tokens); migrations apply cleanly.
- **Mocked-failure tests:** Qdrant down, Upstash down, both embedding keys failing, malformed provider output.
- **Chaos tests:** providers forced to fail; breakers trip and recover.
- **Contract tests:** structured-output schemas against what providers return.
- **Recorded API tests** (VCR.py) for Cohere, Jina, Gemini, OpenRouter and Jev, with keys scrubbed.
- **Coverage target 80%** on gateway, guardrails, cache and memory.
- **Load test (Locust):** start at **10 concurrent users** and step up, reporting where it breaks. Two modes: *stubbed model* (safe; finds code, database and host limits) and *real providers* (uses quota). **Always ask the owner before each run**; show a token estimate first; real-provider runs need an explicit yes.
- Regression: prompts and tools changes run the evaluation core set in CI (alert only).

## 17. Deployment and operations

### 17.1 Hosting
- Render free tier: an API service and a Streamlit service. Free instances sleep when idle and have limited free hours and memory [T: verify current limits].
- A keep-alive pinger prevents idle sleeping (it uses free hours).
- No staging environment.
- Secrets live in Render's secret settings. Eval keys are never on Render.

### 17.2 CI/CD [P]
GitHub Actions: lint, fast unit tests (no network), secret scan, `pip-audit`, the 20-case evaluation as a separate **alert-only** job, deploy on push to the main branch. The test job starts a throw-away Postgres service container for the Postgres-specific tests; Postgres is never installed on the admin machine.

### 17.3 Releases
- Corpus and code ship separately; a re-ingest changes the corpus version, not the code.
- **Rollback by redeploying the previous build, in under 5 minutes, rehearsed once.**
- A lightweight **release record** per change: what changed, evaluation result against baseline, who approved. Prompt, model, retrieval and guardrail changes always get an entry.

### 17.4 Beginner-friendly delivery (required)
The owner is new to deployment. The deliverables must include: a single `DEPLOY.md` with exact click-by-click steps and exact commands; ready-made files (`render.yaml` or Dockerfiles, `.env.example` listing every variable with a plain-language comment, the CI workflow, the migration runner); checklists; what to expect at each step in plain words; and troubleshooting notes.

### 17.5 Runbooks [P]
One page each, with exact steps: all keys down; wrong answer reported; document withdrawn; suspected PII leak; key leaked (rotate); bad deploy (rollback).

### 17.6 Pre-launch gate
Production-readiness score of at least 70/100 with no category below 12/20; a load test passed; a security scan with no open critical or high findings; the red-team set passing; a rollback drill; a cost projection compared with a dry run; the evaluation set and variance band established; the access-control audit; a named business owner.

## 18. Cost

- Free tiers only during the pilot; real spend is expected to be $0.
- Dashboard shows **billed cost** and **shadow cost** (5.4), cost per answer, cost per successful answer, first-pass success rate (grounded, not refused, not escalated), failure multiplier (total spend over spend on successful answers), cache hit rate, and budget variance.
- Cost and per-user budget alerts go to the chat webhook.
- Planning basis: queries per day in the pilot and a monthly budget ceiling [T: no numbers given yet; 100 queries per day used as a placeholder].
- Cohere rerank may be the largest per-query cost; verify its price and trial limits [T].

## 19. Organisation and process

- Owner roles (eval suite, prompt and model changes, document set and entity review, security and compliance, incidents) are all held by the **admin** for now; names are added later. Business owners: 15.6 (placeholder names).
- **Rollout stages:** shadow mode (answers generated and logged but visible only to the admin; planned about two weeks [T]) → limited beta with staff → customers. Each stage has a pass checklist (evaluation inside the band, no open critical alerts, red-team set passing).
- Feedback loop: thumbs-down and refused answers → review queue → evaluation set (11.4).
- Release record per change (17.3).

## 20. Reference test corpus

The company used for sample documents is **AuraVance Digital FinTech Private Limited**.
- **CIN:** `U65110MH2026PTC384152` (the `U…PTC…` format is correct for a private limited company).
- **PAN:** `AAACF2026N`.
- **Domain:** `auravance.aura`.
- Three PDFs of three pages each, generated by a script (each with a table page, an embedded chart and a covenants/disclosures and audit page): a **banking standard facility agreement**, a **mutual fund eCAS and investor servicing guide**, and an **insurance flexi commercial property policy**. All officers, registration numbers, rates and figures are **illustrative**. Compliance must review and replace them with approved data before production use.
- These documents feed the evaluation set.

## 21. Build order [P]

Each phase depends only on earlier phases. The sections each phase covers are in brackets.

1. Foundations: repository layout, the four requirements files, migrations runner, test isolation guard, CI skeleton, `.env.example`. [2, 12.1 migrations, 13.3 secret scan and pip-audit, 16 isolation guard, 17.2, 17.4]
2. Gateway: tiers, key pools, limiter, breaker, backoff, Jev route and fallbacks, token and cost accounting including shadow cost and context windows, the embedding client (two Google keys with failover) and the rerank chain (Cohere, Jina, lexical), both on the same key pools, limiter and breaker, unit tests. [2, 3, 4.1 steps 8 and 10, 5, 6.5, 14 decider/model/key-fingerprint fields, 16]
3. Data layer: Postgres schema for every table in 12.1 (with the session columns of 8.3 and the message flags of 8.5), Qdrant `documents` collection with payload indexes, Upstash client with cache primitives, the corpus-version counter, chunk and document reverse sets and rate-limit counters. [6.4, 7 filter fields, 8.3, 10.3 items 1-2, 11.1 store failures, 12.1, 12.3, 12.4]
4. Accounts, API shell and security: FastAPI app, customer-only self-registration, JWT login with expiry and revocation, login throttling, roles with admin-assigned staff roles, per-user rate limit and daily token budget, admin-only health and public liveness endpoints, CORS, security headers, request size limit, audit rows for login and role change. [1, 4.1 step 1, 5.4 per-user budget, 11.2, 12.1 users/revoked_tokens/audit_log, 13.1, 13.2, 13.3]
5. Ingestion (local): Unstructured parsing, image captions, chunking, stable identity, embeddings through the gateway's embedding client, sparse vectors, incremental reindex, delete with cache invalidation and the "source removed" flag, entity extraction and review, original PDFs kept, failed pages flagged to the review queue. [6, 8.5 flag on delete, 9.3 ingestion scan, 10.3 invalidation on ingest/edit/delete, 11.3 originals kept, 11.4 failed pages, 12.1 allow-list, 20]
6. Guardrails and prompts: query limits, greetings, injection regex and decision, PII masking with the approved allow-list, context guard, output checks (schema, numeric, citation, groundedness), conditional disclaimer, indirect-injection delimiters, prompts as versioned files with a fixed structure and a recorded hash. [4.1 steps 2, 4, 11, 14, 4.2, 9, 13.2]
7. Memory and sessions: summary buffer, persistence, resume, 90-day retention, user delete, session facts, summary clearing on document delete (wired into the delete flow of phase 5), the count of consecutive unclear turns. [4.3 two-unclear-turns state, 4.4, 8, 12.1 sessions/messages, 13.6 user deletes own chats]
8. Retrieval and the LangGraph pipeline: all 17 stages with per-node timeouts, the 30 s budget and early exits, for new and existing sessions (the history-based rewrite uses the memory of phase 7); Jev triage, rewrite, domain gate, exact cache lookup and write with verification, embedding and rerank through the gateway, hybrid retrieval with hard and soft filters, low-confidence branch, generation, persist and respond; review-queue writes for refused and low-confidence answers; per-stage spans and timings. [4, including 4.3 and 4.4, 7, 10.1-10.2, 11.1, 11.4 writes, 14 spans and structured errors, 16 pipeline-branch and mocked-failure tests]
9. Cache hardening and staleness tests: read-time check, role and permission version bumps, sparse-index refresh, all staleness scenarios; the semantic-cache decision, built only if the measured hit rate justifies it. [10, 16 cache and reindex tests]
10. Chat, session and admin API: chat endpoint over the pipeline; session list, resume and delete-my-chats; thumbs feedback; review-queue list and label; user and role management; document-status and entity-review endpoints for the admin machine; alerts-banner data; the shadow-mode switch (answers generated and logged but visible only to the admin). [1, 8.3, 11.4, 12.1 feedback_entries/review_queue, 13.1, 13.6, 19 shadow mode]
11. Streamlit frontend: the owner-approved design first, then every screen of section 24 and the admin tab shells. [1, 4.3, 8.5, 9.4-9.5, 11.1, 13.2 plain-text rendering, 17.1, 24]
12. Observability: Langfuse traces with all audit-grade fields, traces removed by session id, structured JSON logs with correlation id, complete audit log with 30-day retention, alerts (chat webhook and dashboard banner), dev stage printer, dashboards (decider in use, key-pool health, tokens, billed and shadow cost, cache, ingestion health), daily customer and staff metrics. [5.3, 5.4, 12.1 audit_log, 13.6 trace deletion, 14, 15.6, 18]
13. Evaluation: evaluation database (local SQLite, production cloud Postgres), the 60-case set, calibration, RAGAS, DeepEval, light live scorer, variance band, the local-only "Run eval" button, the 5% production sampling queue and weekly drift report, evaluation dashboards, the review queue's "add to evaluation set" button. [11.4, 12.2, 15, 16 regression run, 17.2 20-case job, 20]
14. Testing completion: chaos, contract and recorded (VCR) tests, 80% coverage on gateway, guardrails, cache and memory, load tests (with owner permission). [16]
15. Deployment: Render files for the two services, uptime pinger, Qdrant snapshots, rollback drill, runbooks, key-rotation routine, data-flow map, release-record template, CI deploy step, `DEPLOY.md`. [11.2 pinger, 11.3, 13.3 key rotation, 13.4, 17, 18 cost projection]
16. Baseline run, set numeric targets, pre-launch gate, rollout stages starting with shadow mode. [15 baseline and variance band, 17.6, 18, 19, 23]

## 22. Explicitly out of scope for this version

- A LiteLLM Router or any intent router.
- LlamaIndex, NeMo Guardrails, Guardrails AI, LLM Guard, Prompt Guard, gpt-oss-safeguard.
- Model-driven tool calling, multi-agent behaviour, an MCP server, live-data lookups (policy or claim status).
- Long-term cross-session memory.
- Image or voice input from users.
- A staging environment.
- Admin MFA and a hash-chained audit log (later).
- Email alerts.
- LiteLLM-level response caching.

## 23. Open items before launch

- Meaning of the 10-character minimum (hard refusal, or first-message clarification only).
- What counts as "only when relevant" for the disclaimer.
- Queries per day and the monthly budget ceiling; the staff token budget.
- Review-queue overdue limit and shadow-mode length.
- Terms-of-service review for several accounts per provider; Upstash, Render and Jev limits and prices.
- Names for the owner roles (placeholders are used).
- Which OpenRouter model is the judge.
- Langfuse retention on the chosen plan.
- Confirmation of every [P] item.
- Which admin tabs run on the admin machine and which in production (section 24).
- How scheduled and background work runs on the free host: 90-day chat deletion, 30-day audit deletion, the 5% evaluation queue and its judge, the weekly drift report, alert checks.
- Where the original PDFs and the Qdrant snapshots are stored (section 11.3).
- The Decisions API is alpha; free-tier limits of Gemini 3.8 Flash and Gemini Embedding 2 are unpublished.

## 24. Streamlit frontend

- **Quality bar:** beautiful and polished, with an outstanding UI and every required feature below. Before building, the design (palette, typography, layout, light and dark theme, main screens, component list) is proposed to the owner and approved.
- **Rendering:** app-authored markup may use custom CSS and HTML. Model output is always rendered as plain text, never as HTML or links.
- **Features (all roles):** login and self-registration (customer role only); chat with cited clauses; confidence and warning banners; degraded-mode notices; "source removed" flags on old messages; clarification prompts; session list with resume and delete-my-chats; thumbs feedback.
- **Admin-only tabs:** documents (ingest status, delete); entity review; human-review queue; user and role management; alerts banner; eval dashboard (local versus production, drift, decider in use, key-pool health, billed versus shadow cost).
- **Where each tab runs [P]:** one codebase. The ingest, delete, entity-review and "Run eval" controls appear only when the app runs on the admin machine. The review queue, users and roles, alerts banner and the production evaluation view are available to admins in production. [T: confirm]
- The dev stage printer stays terminal-only.
