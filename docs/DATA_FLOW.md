# Data-flow map: who sees what

Spec 13.4. One page, kept true. It says what each outside vendor receives, from the code as it is
now. Two rules sit under it: personal data is masked **locally, before any model call** (spec 9.2),
and **no real customer data** is used until paid or zero-retention plans are in place (spec 13.5).

**Masked** means the question has had names, phone numbers, e-mail addresses and identifier
numbers replaced by placeholders (`[PAN]`, `[PHONE]` ...) by the masker inside the API process.
Document text and company terms on the allow-list are not masked.

| Vendor | Receives | Does not receive | Why it is needed |
|---|---|---|---|
| **Render** (hosting) | the traffic to the API and the page, as it arrives (the RAW question is in transit and in the API's memory until it is masked), the service's settings and secrets, the service's logs | nothing is stored on its disk by the application; the logs carry no text of a question or answer | runs the two services |
| **Groq** (answers and decisions) | the masked question, the retrieved passages, the system prompt; for a follow-up the masked summary and the last turns (rewrite step only: the answer step never sees history) | the raw question, a user name, an e-mail address, an account id | answer and decision models |
| **Google Gemini** (answers, decisions, image captions, embeddings) | the same as Groq for answers and decisions; the masked question to embed it; at ingestion (your machine) the document text and the page images | the raw question, a user name | fallback answer model, embeddings, captions |
| **OpenRouter** (answers, decisions; also Jev) | the masked question and the retrieved passages, the typed decision questions | the raw question, a user name | fallback answer model and the Jev decision model |
| **Jev** (through OpenRouter's Decisions API, its own key) | the masked question and the state of the typed questions (domain, vagueness, injection, filters) | the raw question, passages are not needed for decisions | quick yes/no and choice decisions |
| **Cohere** and **Jina** (rerank) | the masked question and the retrieved passages | the raw question, a user name | orders the passages |
| **Qdrant** (vector store) | the document vectors and the chunk text and its labels (company documents) | no customer chat, no user data; a question is sent only as a vector | finds passages |
| **Upstash** (cache and limits) | cached grounded answers keyed by a hash of the masked question, rate-limit counters keyed by hashed addresses and names, the corpus version | the raw question, a user name or address in clear | speeds up and limits |
| **Neon** (Postgres, main database) | accounts (username, e-mail, password hash), masked transcripts, summaries, feedback, the review queue with MASKED questions, audit rows with keyed hashes | the raw question | the system's memory |
| **Neon** (second database, evaluation) | the masked 5 % sample of live answers: masked question and answer, passages, counts; no user or session id; scores | any user identifier, the raw question | the production half of evaluation |
| **Langfuse** (tracing) | per-stage records: stage names, timings, decider, model, key fingerprint, token counts, flags, prompt hashes, the corpus version; hashed user and session names | the question, the answer, passage text, history, a username, an e-mail address | finds slow or failing stages |
| **The chat webhook** | the alert's name, severity, a count and a window | any text or identifier | tells you something is wrong |
| **GitHub** | the code, the CI logs | no data, no secret (secrets are not in the repository; a scan checks) | source and checks |

## What stays on your machine only

The original PDFs (`originals/`), the model-answer cache of ingestion, the evaluation keys and the
local evaluation database, the entity review, every load test, and any `.env` file. Evaluation runs
use the separate evaluation keys and a separate test collection of the vector store.

## When this page must change

A new vendor, a new field sent to a vendor, a new thing stored in the main database, or a change of
what is masked. A change of this kind is a release record of kind `guardrail` or `config`.
