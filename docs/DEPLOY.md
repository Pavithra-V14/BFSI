# Putting the assistant on the internet (DEPLOY.md)

This guide is for a first deployment. It says what to click, what to type and what you should
see, in plain words. Nothing here is run by Claude: **you** do each step, and each step that
needs a real service is also a numbered run in `docs/OWNER_RUNS.md` (Runs 28 to 34).

If a step does not look the way it says, stop and read "When something is not right" at the end,
or send the exact line you see (never a key or an address) to your helper.

---

## 0. What you are building

```
 your browser ──► bfsi-frontend (Streamlit, Render) ──► bfsi-api (FastAPI, Render)
                                                          │
          ┌──────────────┬─────────────┬─────────────┬────┴────────┐
          ▼              ▼             ▼             ▼             ▼
       Neon           Qdrant        Upstash       providers     Langfuse
    (Postgres)      (documents)   (cache, limits) (Groq, ...)   (traces)
```

- **Two services on Render's free plan.** The API answers questions. The frontend is the web page.
- **Everything with data is outside Render**: the database (Neon), the documents (Qdrant), the
  cache (Upstash). A Render service can be thrown away and rebuilt at any time.
- **Documents are put in from your own machine** (ingestion never runs on Render).

### Facts about Render's free plan that you must know [T: verify on Render's pricing page]

These are stated from memory of Render's terms and are **placeholders to verify**: check the
current limits on Render's pricing page before you rely on them.

- A free web service **goes to sleep after about 15 minutes without a visit** and takes about a
  minute to wake. The first person after a quiet spell waits.
- A free account has about **750 instance hours a month for all free services together**. One
  service awake all month is about 730 hours; two would need about 1,490, so they cannot both
  stay awake all month. That is why the pinger (below) works only inside a stated window.
- A free instance has **512 MB of memory** and a small share of one CPU.
- **The disk is ephemeral.** Anything written to the service's own disk is gone at every restart
  or deploy. See section 7.
- There are **no cron jobs** on the free plan. The scheduled jobs run on your own machine
  (section 9).

---

## 1. Before you start: what you need

| You need | Where | What you do |
|---|---|---|
| A GitHub account with this repository | github.com | already there |
| A Render account | render.com | sign up with GitHub |
| A Neon project (Postgres) | neon.tech | already made for the main database; make a second small database for evaluation |
| A Qdrant Cloud cluster | cloud.qdrant.io | already made |
| An Upstash Redis database | upstash.com | already made |
| Provider keys | Groq, Google AI Studio, OpenRouter, Cohere, Jina | **one key per provider, from one account you own** (`docs/READINESS.md`, terms of service) |
| A Langfuse project | langfuse.com | keys and your region's address |
| A chat webhook address | your team's chat (Slack, Teams or Discord) | for alerts |

Make the three secrets **you** invent, each in your own terminal, a different one each time (see
the comment at JWT_SECRET_KEY in `.env.example`):

```
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Run it three times: for `JWT_SECRET_KEY`, `AUDIT_HASH_KEY` and `LANGFUSE_USER_HASH_KEY`. Never put
them in a chat, a file in this folder or a ticket.

---

## 2. Step by step: the first deploy (Run 28)

### 2.0 The helper that reads a secret (once per PowerShell window)

Several commands below read a secret with `Read-Secret`. It is not a Windows command: paste this
into the window first. It asks for the value with a hidden prompt, so it never appears on screen
or in your command history (the same function is in `docs/OWNER_RUNS.md`, "Common setup"):

```
function Read-Secret($name) {
  $s = Read-Host "$name (hidden)" -AsSecureString
  $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
  try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}
```

### 2.1 The database tables (from your machine, once)

1. In Neon, open your main database and copy the **direct** connection string (the host does NOT
   contain `-pooler`; it ends with `?sslmode=require`).
2. In PowerShell, in the project folder:

```
$env:DATABASE_URL = Read-Secret "DATABASE_URL"
uv run python -m app.db.migrate --dry-run
uv run python -m app.db.migrate
uv run python -m app.db.migrate --dry-run
```

You should see the pending files listed, then them applied, then nothing pending. The last file
is `0010_withdrawn_documents.sql`: the small table that remembers which documents were withdrawn
on purpose, so that a restore can never bring one back (`docs/runbooks/restore-documents.md`).

3. Do the same for the evaluation database with `uv run python -m app.eval migrate --production`
   after setting `EVAL_DATABASE_URL` the same way.
4. Create the first admin, in the same window (`DATABASE_URL` is still set; this command needs
   nothing else): `uv run python -m app.accounts.admin_cli create-admin --username NAME
   --email ADDRESS` (it asks for a password in a hidden prompt, twice). Then clear the window's
   address: `Remove-Item Env:DATABASE_URL`.

### 2.2 The documents (from your machine)

Create the collection and ingest the documents as in `docs/OWNER_RUNS.md` Runs 2 and 10. Keep
`originals/` safe: it is the only copy of the PDFs (section 8).

### 2.3 Fill in the region in `render.yaml`

Open `render.yaml`. In both services replace `REPLACE_WITH_REGION_NEAR_YOUR_NEON_DATABASE` with
the word of the Render region that is closest to your Neon database. Neon's own dashboard shows
its region. Each chat answer makes several database calls, so a far region makes every answer
slower. The words Render's blueprint file accepts, in lower case [T: verify on Render's
documentation for `region` in a blueprint, the list may have changed]:

| Your Neon region | Write this in `render.yaml` |
|---|---|
| US East (N. Virginia) | `virginia` |
| US East (Ohio) | `ohio` |
| US West (Oregon) | `oregon` |
| Europe (Frankfurt) | `frankfurt` |
| Asia Pacific (Singapore) | `singapore` |
| Europe (London) | `frankfurt` (nearest) |
| Asia Pacific (Mumbai) | `singapore` (nearest; Render has no region in India) |

Write it the same way in both services. Commit that one change. Render refuses the file until you
do, which is the point. If your Neon region is not in the table, pick the Render region on the
same continent and send the two names to your helper.

### 2.4 Create the services on Render

1. Render dashboard, **New**, **Blueprint**, choose this repository, branch `main`.
2. Render reads `render.yaml` and lists two services, `bfsi-api` and `bfsi-frontend`, and asks you
   for every variable marked as a secret. Fill them from the table in section 5. The API needs the
   values of the "required" rows; a provider you do not have (Jina, a second embedding key) can be
   left empty.
3. Two values cannot be known yet, because each is the address of the OTHER service and Render
   shows an address only after the service exists. Leave them empty (or type a placeholder
   word) in the form:
   - `API_BASE_URL` of `bfsi-frontend`: the address of `bfsi-api`;
   - `CORS_ALLOWED_ORIGIN` of `bfsi-api`: the address of `bfsi-frontend`.
4. Wait for the first build of both (several minutes). **Expect `bfsi-frontend` to show Failed
   at first**: on Render it refuses to start without an `https://` `API_BASE_URL` (the message
   names the variable only). That is the safety check working, not a fault. `bfsi-api` may
   also show a failed start until its own settings are complete; read its log line
   `refusing to start: NAME` if it does.
5. Now fill the two addresses. For each service: **Environment**, edit the variable, **Save**
   (Render restarts the service, about a minute).
   - Copy the address from the top of the other service's page, exactly as shown. It must be the
     origin only: `https://` then the host name, **no path and no trailing slash**
     (`https://bfsi-frontend-abcd.onrender.com`, not `.../`). The API refuses to start on a
     trailing slash, a path, a list or `*`, naming the variable only.
   - `API_BASE_URL` may have no trailing slash either.
6. Wait for both to show **Live**.

### 2.5 Check that it is alive

In Windows PowerShell write `curl.exe`, not `curl`: plain `curl` is an alias of another command
there and takes different options.

```
curl.exe https://YOUR-API.onrender.com/live
```

You should see `{"status":"ok"}`. Open the frontend address in a browser: you should see the
sign-in page. Sign in as the admin of 2.1.

### 2.5a A token for the checks below (once per PowerShell window)

The next steps call the admin routes of the API, which need a sign-in token. Get one without
typing the password or the token into a command (what you type is kept in PowerShell's history).
Paste the `Read-Secret` helper of 2.0 into the window first, then:

```
$api = "https://YOUR-API.onrender.com"
$login = @{ username = Read-Host "admin username"; password = Read-Secret "admin password" }
$reply = Invoke-RestMethod -Method Post -Uri "$api/auth/login" -ContentType "application/json" `
    -Body ($login | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($reply.access_token)" }
Remove-Variable login, reply
```

Nothing is printed. `$headers` now holds the token for this window only. The token lasts 8
hours (`JWT_EXPIRE_MINUTES`, default 480). **When you are done with the checks, revoke it and
clear the window:**

```
Invoke-RestMethod -Method Post -Uri "$api/auth/logout" -Headers $headers
Remove-Variable headers
```

If the first command answers `401`, the username or password is wrong (the same answer for
both, on purpose). Do not show the value of `$headers` to anyone.

### 2.5b Which window does which admin job

Rollout, users, documents and the review queue are done in the **hosted** frontend, signed in as
the admin. Ingesting, deleting and entity approval are done on the **admin machine**. Exactly:

| Job | Where | How |
|---|---|---|
| Rollout stage (shadow, staff beta, open), Users and roles, Documents (counts), Review queue, Alerts banner, Dashboards, Evaluation numbers (read-only) | the hosted frontend (its Render address) | sign in as the admin; the sections appear in the sidebar. `ADMIN_MACHINE` is not set on Render and must not be |
| Ingest and delete a document, restore, snapshots, retention, evaluation runs | the admin machine's terminal | the commands of this guide and `docs/OWNER_RUNS.md`; the browser never starts them |
| The words of the ingest and delete commands, shown ready to copy; the entity buttons | a **local** frontend pointed at the Render API | `$env:API_BASE_URL = "https://YOUR-API.onrender.com"; $env:ADMIN_MACHINE = "true"; uv run streamlit run frontend/app.py`. It shows only the admin-machine controls on top of the same sections |
| Approve or reject an allow-list entity | the admin machine's terminal, **command line only** | `uv run python -m app.ingest review entities --doc-id SLUG`. **The buttons of the local frontend do not work against Render**: the hosted API refuses entity approval whatever the setting (spec 10), so the command line is the only way |

A local frontend asks for the admin's sign-in like the hosted one and is the same program, only
with a different `API_BASE_URL`.

### 2.6 Check the client address (the proxy setting)

Render puts its own proxy in front of the API. The API must count limits per real user address,
not per Render's proxy. **`render.yaml` ships with `TRUSTED_PROXY_HOPS` at 0** (the header is not
read, everyone shares one limit: safe, only coarse) until you have looked at what Render really
sends. Call the diagnostics route with the token of 2.5a:

```
Invoke-RestMethod -Uri "$api/admin/diagnostics" -Headers $headers | Format-List
```

You should see `trusted_proxy_hops` of 0, `forwarded_entries` of 1 or more, and
`address_at_one_hop` equal to **your real public address** (ask a "what is my IP" page, from the
same network you called from). `socket_address` is Render's proxy, not you.

- If `address_at_one_hop` **is** your public address: Render's proxy writes the address it saw as
  the last entry. In the Render dashboard, `bfsi-api`, **Environment**, set `TRUSTED_PROXY_HOPS`
  to `1`, **Save**, wait for the restart, call the route again: `address_source` is now
  `forwarded` and `client_address_used` is your address. Also change the value in `render.yaml`
  (so a rebuilt blueprint keeps it) and commit that one line.
- If it is **not** your address (it is Render's own, or an address you do not know), or
  `forwarded_entries` is 0: leave it at 0 and send the numbers shown (never the whole address, the
  first two groups are enough) to your helper. Do not set 1.

### 2.7 Check the memory

The same page shows `memory_mb` and `peak_memory_mb`. Do this once the service has answered a
first question (the name model is then loaded): open the frontend in two tabs, and in one of them
sign out and sign in again (this runs a password hash) while the other asks a question. Read
`/admin/diagnostics` again straight away (the same `Invoke-RestMethod` line as 2.6; if that
sign-in changed your window's token, repeat 2.5a). If `peak_memory_mb` is above about 450, see the memory
fallback order in section 7.

### 2.8 The first deploy runs in SHADOW mode

A new, empty database starts in the rollout stage `shadow`: answers are generated and stored, but
only the admin sees them; everyone else sees a fixed "test period" notice. **Leave it there.**
Moving to `staff_beta` (staff see answers) and then `open` (everyone) is a deliberate step with a
checklist (section 6). Nothing in this guide moves it for you.

**But the database you use may not be new**: it is the one you used for Runs 1 to 22, and the
stage may have been moved there. Read it, and set it back if it is not `shadow`:

```
Invoke-RestMethod -Uri "$api/admin/shadow" -Headers $headers
```

You should see `stage` of `shadow`. If it shows another stage, set it before anyone else signs
in (the Rollout section of the hosted frontend does the same):

```
Invoke-RestMethod -Method Put -Uri "$api/admin/shadow" -Headers $headers `
    -ContentType "application/json" -Body '{"stage":"shadow"}'
```

### 2.9 The pinger and the check after a deploy

1. In GitHub, **Settings, Secrets and variables, Actions, Variables**: add `RENDER_API_URL` and
   `RENDER_FRONTEND_URL` (the two https addresses; they are not secrets).
2. The workflow `Keep the free services awake` then pings both inside 08:00 to 20:00 India time,
   Monday to Friday. Honest limits: GitHub runs scheduled workflows on a best-effort basis (a run
   can be late or skipped), and **switches a scheduled workflow off after 60 days without any
   activity in the repository** (any commit switches it back on).
3. After a push to `main` has passed the CI checks, the separate workflow `After a deploy` waits
   five minutes, then asks `/live` for up to 12 minutes and goes **red** if the API never answers.
   It is an alert on `/live` only: it cannot tell the new build from the old one (the liveness
   answer reveals nothing, by design), so green means "the service is up". It is a separate
   workflow on purpose: Render waits for every check of a commit before it deploys, and a check
   that failed while the service was down would hold the very fix.
4. Both workflows **fail visibly** (a red run; GitHub notifies the person who last changed the
   workflow, according to their notification settings). Neither blocks anything.

---

## 3. Deploys, and going back

- Render deploys on its own **after the CI checks pass** on `main` (`autoDeployTrigger:
  checksPass`). No Render secret is stored in GitHub.
- **This is the one gate, and it is a documented exception** to the rule that no check blocks a
  release (spec 17.2): a failing check holds only the AUTOMATIC deploy. It never stops you.
- **To release anyway** (a check is red and you decide to ship, or a fix must go out now): Render
  dashboard, `bfsi-api`, **Manual Deploy**, **Deploy a specific commit**, pick the commit (or
  **Deploy latest commit**), **Deploy**. Do the same on `bfsi-frontend` if it changed. Write why in
  a release record (`docs/releases/`): a release that skipped a red check always gets one.
- **Going back** (the rehearsed rollback, under 5 minutes): Render dashboard, the service,
  **Deploys**, pick the last good deploy, **Rollback**. Then ask `/live`. Details and the
  rehearsal record: `docs/runbooks/bad-deploy-rollback.md` (Run 29).
- **After a Rollback, check that Auto-Deploy is on again** [T: verify on the day]. Render may
  switch automatic deploys off for a service when you roll it back, so that the next push does
  not undo the rollback. Open the service's **Settings**, find **Auto-Deploy** (or the
  `autoDeployTrigger` field), and put it back to "After CI checks pass" when you want pushes to
  deploy again. Until you do, a fix on `main` is NOT deployed by itself: use **Manual Deploy**.
- Corpus and code ship separately: a re-ingest changes the corpus version, not the code.
- Every prompt, model, retrieval or guardrail change gets a release record:
  `uv run python scripts/release_record.py --title "..." --kind prompt` (section "Release
  records" in `docs/releases/README.md`).

---

## 4. The database address: direct for now

The running API uses the **direct** database address (the one without `-pooler`). Do not switch
it to the pooled address on your own. The reason is on purpose and written down:

- The code already turns off server-side prepared statements, which is what a pooled address
  needs, but it also relies on session settings (the isolation level set on each connection and
  start-up options) that a transaction-pooling proxy may not carry from one request to the next.
- That has not been tested on a real pooled run. Until it has (a deliberate test that checks the
  isolation setting and the start-up options on a pooled connection), the pooled address is not
  recommended.
- First measure: Run 32 (`scripts/db_connections.py`) shows how many connections are open
  against the plan's limit. Only if that count is near the limit is the pooled address worth a
  deliberate test.

---

## 5. Every Render variable (names only)

Source = where you get the value. Secret = a value that must never be written in a file, a chat
or a log. "In render.yaml" = already listed there; if "no", add it in Render's Environment tab only
if you want to change the default. A variable left out, or left empty, uses its default
(`.env.example` says what it is).

| Variable | Where the value comes from | Secret? | In render.yaml? | Needed? |
|---|---|---|---|---|
| `GROQ_API_KEYS` | your Groq account (console): one key; a second only while a rotation overlaps | yes | yes | required |
| `GEMINI_API_KEYS` | your Google AI Studio account: one key (see the rule above) | yes | yes | at least one provider key |
| `OPENROUTER_API_KEYS` | your OpenRouter account: one key (see the rule above) | yes | yes | at least one provider key |
| `JEV_OPENROUTER_API_KEY` | its own OpenRouter key | yes | yes | optional |
| `GEMINI_EMBEDDING_KEY_A` | a Google account (embeddings) | yes | yes | required |
| `GEMINI_EMBEDDING_KEY_B` | a second key from the SAME account, only for the overlap of a rotation (quota is per project, so it adds no capacity) | yes | yes | optional |
| `COHERE_API_KEY` | Cohere dashboard | yes | yes | optional |
| `JINA_API_KEY` | Jina dashboard | yes | yes | optional |
| `QDRANT_URL` | Qdrant Cloud cluster page | yes | yes | required |
| `QDRANT_API_KEY` | Qdrant Cloud, API keys | yes | yes | required |
| `DATABASE_URL` | Neon, connection string (direct address, ends with sslmode=require) | yes | yes | required |
| `EVAL_DATABASE_URL` | a second Neon database used for evaluation only | yes | yes | optional |
| `EVAL_SAMPLE_RATE` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `UPSTASH_REDIS_URL` | Upstash Redis page (starts with rediss://) | yes | yes | required |
| `CACHE_TTL_SECONDS` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `LANGFUSE_PUBLIC_KEY` | Langfuse project settings | yes | yes | optional |
| `LANGFUSE_SECRET_KEY` | Langfuse project settings | yes | yes | optional |
| `LANGFUSE_HOST` | Langfuse (your region's address) | no | yes | optional |
| `LANGFUSE_USER_HASH_KEY` | you make it (a third random key) | yes | yes | optional |
| `JWT_SECRET_KEY` | you make it (a random key, see .env.example) | yes | yes | required |
| `AUDIT_HASH_KEY` | you make it (a second, different random key) | yes | yes | required |
| `JWT_EXPIRE_MINUTES` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `TOKEN_BUDGET_CUSTOMER` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `TOKEN_BUDGET_STAFF` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `PASSWORD_HASH_CONCURRENCY` | set to 2 in render.yaml | no | yes | optional |
| `TRUSTED_PROXY_HOPS` | ships as `0` in render.yaml; set `1` only after the check of 2.6 | no | yes | optional (0 is safe, only coarse) |
| `CORS_ALLOWED_ORIGIN` | the address Render gives bfsi-frontend: origin only, https, no path, no trailing slash; of `bfsi-api` only | no | yes | required |
| `ALERT_WEBHOOK_URL` | your team chat's incoming-webhook address | yes | yes | optional |
| `ALERT_WEBHOOK_FORMAT` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `ALERT_WINDOW_MINUTES` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `ALERT_MIN_REQUESTS` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `ALERT_ERROR_RATE` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `ALERT_FALLBACK_RATE` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `ALERT_GUARDRAIL_RATE` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `ALERT_DAILY_TOKEN_PLAN` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `ALERT_BUDGET_VARIANCE` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `ALERT_WEBHOOK_DAILY_CAP` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GROQ_MODEL` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GROQ_DECIDE_MODEL` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GEMINI_MODEL` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GEMINI_DECIDE_MODEL` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `OPENROUTER_MODEL` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `OPENROUTER_DECIDE_MODEL` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `JEV_MODEL` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GEMINI_EMBEDDING_MODEL` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `COHERE_RERANK_MODEL` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `JINA_RERANK_MODEL` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_LIMITS_GROQ` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_LIMITS_GEMINI` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_LIMITS_OPENROUTER` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_LIMITS_JEV` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_LIMITS_EMBEDDING` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_LIMITS_COHERE` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_LIMITS_JINA` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_PLAN_GROQ` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_PLAN_GEMINI` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_PLAN_OPENROUTER` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_PLAN_JEV` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_PLAN_EMBEDDING` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_TIMEOUT_SECONDS` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_CONTEXT_OVERRIDES` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_REASONING_ALLOWANCE_TOKENS` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_REASONING_EFFORT_ANSWER` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `GATEWAY_REASONING_EFFORT_DECIDE` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |
| `COMPANY_DOMAINS` | the company's real domains, comma separated | no | yes | required |
| `API_BASE_URL` | the address Render gives bfsi-api (https, no trailing slash); of `bfsi-frontend` only | no | yes | required on `bfsi-frontend` (it refuses to start without it) |
| `MEMORY_BUFFER_TOKENS` | optional tuning: leave it out and the default is used | no | no (add it in Render if you want it) | optional |

### Variables that must NOT be set on Render

These belong to your own machine. The gateway refuses to start on Render with an evaluation key or
the mock switch, and `render.yaml` holds none of them (a test checks it):

`EVAL_GROQ_API_KEYS`, `EVAL_GEMINI_API_KEYS`, `EVAL_OPENROUTER_API_KEYS`, `EVAL_JUDGE_MODEL`, `EVAL_COLLECTION`, `INGEST_COLLECTION`, `DEV_STAGE_TRACE`, `GATEWAY_ENABLE_MOCK`, `COMPANY_NAMES`, `ENTITY_REVIEW_ENABLED`, `ADMIN_MACHINE`

Two names that start with `EVAL_` ARE set on Render on purpose: `EVAL_DATABASE_URL` and
`EVAL_SAMPLE_RATE`, because the running service writes the masked 5 % sample (Phase 13).

---

## 6. Moving from shadow to staff beta to open

Only the admin can do it, with `PUT /admin/shadow` (the Rollout section of the admin page of
the hosted frontend; see 2.5b). Each move is deliberate. Before `staff_beta`, every line must be true:

- [ ] Run 21 is done: a baseline run and the variance band exist, and the golden score is inside
      the band.
- [ ] Run 22 is done: the production sample is written (masked) and judged.
- [ ] Run 25 (cassettes) and Run 27 (a short real-provider run) are done and clean.
- [ ] No critical alert in the last 7 days; the retention task has run in the last 7 days.
- [ ] The red-team set passes (Phase 16).
- [ ] The release record for this stage exists in `docs/releases/`.
- [ ] You have read the data-flow page (`docs/DATA_FLOW.md`) and agree with it.

Before `open`: all of the above again, plus a named business owner and the compliance advisor's
answer on the data terms (spec 13.5, 13.7). Real customer data waits for paid or zero-retention
plans.

---

## 7. What resets when the service restarts

Render's free disk is ephemeral, and the API keeps some things in its own memory. After every
deploy, restart or sleep these are **empty again**, by design:

- **The disk.** The API must write nothing it needs to a local path. A test scans the hosted
  code for that. Logs go to Render's log stream. The original PDFs, the local model-answer cache
  and the evaluation database are on your machine, never on Render.
- **In-process state:** the live numbers and the alert rules' windows (the saved hourly and daily
  rollups and the alerts already written are in the database and survive); the 5-second rollout
  stage cache; the per-session locks; the event buffer behind the admin banner; the persist queue
  (a chat answer queued for the database when the database was down is **lost** if the service
  restarts before it is written; the alert `persist_job_dead_lettered` names a failed one).
- **A restart signs everyone out of nothing:** sign-in tokens are signed, not stored; they stay
  valid until they expire. A browser refresh signs a user out of the Streamlit page (by design).

If the API runs out of memory (512 MB), the fallback order is: **first** lower
`PASSWORD_HASH_CONCURRENCY` (each running hash uses about 19 MB; 2 is the default, 1 is the
floor); **then** move the API to a larger Render instance. Masking is **never** turned off to save
memory.

---

## 8. Backups

- **Documents**: the TESTED route is a re-ingest from `originals/`. `originals/<doc_id>/<sha256>.pdf`
  is the only copy of the PDFs. **Back that folder up yourself** (an external drive and a second
  copy somewhere else), including `originals/_withdrawn/`, and rehearse one restore:
  `docs/runbooks/restore-documents.md`.
- A Qdrant snapshot (`scripts/qdrant_snapshot.py`, Run 30) is a second route that is **unproven**
  until Run 30 has passed on your cluster: restoring a downloaded snapshot has not been done.
  Snapshots also still hold the text of a document withdrawn after they were taken, until they
  age out (known limitation).
- **A restore must never bring a withdrawn document back.** The delete command records every
  withdrawn id in the database and moves its PDFs to `originals/_withdrawn/`; both restore paths
  end by running `uv run python -m app.ingest withdrawn` and `delete` again for any id it names.
- **The database** is not covered by this section: see the next point.
- **Postgres** (accounts and chats): Neon keeps its own history for a limited time (see your Neon
  plan [T]); there is no export step in this project. A deleted or damaged Neon project loses the
  accounts and chats. This is a known limitation of the free pilot.

---

## 9. Scheduled jobs on your machine (Windows Task Scheduler)

Render's free plan has no cron. These run on the admin machine. Each needs secrets in its own
shell; they are NOT read from `.env` by the program (`.env` files are never read). Use the
pattern in 9.1.

| Task | When | Command | Shell secrets it needs |
|---|---|---|---|
| BFSI retention | every day, 03:00 | `uv run python -m app.memory.retention` | `DATABASE_URL` (direct), `LANGFUSE_USER_HASH_KEY`, `EVAL_DATABASE_URL` |
| BFSI drift | Monday, 07:00 | `uv run python -m app.eval drift --production` | `EVAL_DATABASE_URL` |
| BFSI snapshot | Sunday, 02:00 | `uv run python scripts/qdrant_snapshot.py create --run`, then `... download --newest --run`, then `... prune --keep 5 --run` (in this order; stop at the first failure) | `QDRANT_URL`, `QDRANT_API_KEY` |

Not scheduled on purpose:

- **Judging the production sample** (`app.eval judge-samples`) needs the code of its own estimate
  and spends the free tiers: it is a manual step, weekly, after the drift report.
- **Load tests** are only ever run by you, with your yes.

Prune deletes only an older snapshot that has a downloaded copy of the same size in
`backups/qdrant/`, which is why the download comes before it. Without the download step the
snapshots would pile up on the cluster and never be removed, and no copy would exist off the
cluster.

The retention task writes one `retention_sweep` row each time it runs. When the newest such row is
more than seven days old, the service raises the warning alert `retention_overdue` (the admin
banner shows it). The task runs daily, so a week of quiet means the machine was off or the task
failed.

### 9.1 How a scheduled task gets its secrets

Do not type a secret into the task's definition. Store each one once, encrypted for your Windows
user, then let the task read them:

```
# once, in your own PowerShell (it asks for the value, nothing is echoed):
New-Item -ItemType Directory -Force "$env:USERPROFILE\bfsi-secrets" | Out-Null
Read-Host "DATABASE_URL" -AsSecureString | ConvertFrom-SecureString |
    Set-Content "$env:USERPROFILE\bfsi-secrets\DATABASE_URL.txt"
```

Do that once for every name the three tasks need (`DATABASE_URL`, `LANGFUSE_USER_HASH_KEY`,
`EVAL_DATABASE_URL`, `QDRANT_URL`, `QDRANT_API_KEY`): five files, each made with the two lines
above and the right name.

Each task runs a small script kept **outside this repository folder**. All three start the same
way (the reading of the encrypted files), write what they print to a log file (counts and names
only, never a secret) and stop at the first failing command:

```
# C:\Users\YOU\bfsi-tasks\retention.ps1
$log = "$env:USERPROFILE\bfsi-tasks\retention.log"
Set-Location "E:\Claude\BFSI"
foreach ($name in "DATABASE_URL", "LANGFUSE_USER_HASH_KEY", "EVAL_DATABASE_URL") {
    $secure = Get-Content "$env:USERPROFILE\bfsi-secrets\$name.txt" | ConvertTo-SecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    Set-Item -Path "Env:$name" -Value $plain
}
"--- $(Get-Date -Format s)" | Add-Content $log
uv run python -m app.memory.retention *>> $log
exit $LASTEXITCODE
```

```
# C:\Users\YOU\bfsi-tasks\snapshot.ps1  (same reading loop with "QDRANT_URL", "QDRANT_API_KEY")
$log = "$env:USERPROFILE\bfsi-tasks\snapshot.log"
Set-Location "E:\Claude\BFSI"
# ... the foreach loop of retention.ps1, for the two names above ...
"--- $(Get-Date -Format s)" | Add-Content $log
uv run python scripts/qdrant_snapshot.py create --run *>> $log
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run python scripts/qdrant_snapshot.py download --newest --run *>> $log
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run python scripts/qdrant_snapshot.py prune --keep 5 --run *>> $log
exit $LASTEXITCODE
```

```
# C:\Users\YOU\bfsi-tasks\drift.ps1  (same reading loop with "EVAL_DATABASE_URL")
$log = "$env:USERPROFILE\bfsi-tasks\drift.log"
Set-Location "E:\Claude\BFSI"
# ... the foreach loop of retention.ps1, for the name above ...
"--- $(Get-Date -Format s)" | Add-Content $log
uv run python -m app.eval drift --production *>> $log
exit $LASTEXITCODE
```

Only the retention task has an alert when it stops running (`retention_overdue`). The drift and
snapshot tasks have none: look at the end of their log files when you do the weekly check, and
look at **Last Run Result** in Task Scheduler (0 is success). A snapshot task that fails
silently means no backup, so check it the week after you create it.

**When you rotate a key, replace its file too** (`docs/KEY_ROTATION.md`, `docs/runbooks/key-leaked.md`):
run the two lines above again with the same name. Otherwise the scheduled task keeps using the
old, revoked value and fails without a sound.

The encrypted files can only be read by the same Windows user on the same machine. Add the task
in Task Scheduler (**Create Task**, run whether the user is logged on or not, action
`powershell.exe -NoProfile -File C:\Users\YOU\bfsi-tasks\retention.ps1`). Run it once by hand
first and read its output: it prints counts only. The scan `uv run python -m
app.observability.scan LOGFILE` is for you to run on a log file by hand when you suspect a leak.

---

## 10. Cost

The pilot is expected to cost $0. `uv run python scripts/cost_projection.py` shows the planned
numbers for 100 queries a day (a [T] placeholder) and `docs/COST_PROJECTION.md` explains them.
Phase 16 compares the projection with a dry run.

---

## 11. When something is not right

| What you see | What it means | What to do |
|---|---|---|
| Render says the Blueprint is invalid | the region placeholder is still in `render.yaml` | section 2.3 |
| The API build fails on `en_core_web_sm` | the file's SHA-256 changed or the download failed | the build refuses a file that does not match the hash in `requirements.txt` on purpose; re-run the build once; if it persists, send the line to your helper |
| `/live` does not answer | the service is asleep or still starting | wait one minute and ask again |
| `refusing to start: ...` in the API logs | a setting is missing or wrong; the line names the variable | fix that variable in Render's Environment tab |
| Everyone shares one limit / register throttled for all | the client address is Render's proxy | section 2.6 |
| The frontend says it cannot reach the API | `API_BASE_URL` is wrong or not https | fix it on `bfsi-frontend` |
| The admin banner shows `retention_overdue` | the scheduled retention task did not run | section 9 |
| Answers say "temporarily unavailable" | every provider is failing or limited | `docs/runbooks/all-keys-down.md` |
| Out-of-memory restarts | the 512 MB host is full | section 7 |

The seven runbooks and their index are in `docs/runbooks/`.
