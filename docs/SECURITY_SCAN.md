# Security scan (pilot)

Spec 17.6: "a security scan with no open critical or high findings". The scan is made of three
tools, each with a known scope. All of it is **alert only** (build rule 5): a finding turns a CI
job red, it never blocks a merge or a release.

| Tool | What it looks at | Where it runs | Result recorded in |
|---|---|---|---|
| **pip-audit** | known vulnerabilities in the pinned packages | CI: `requirements.txt` and `-dev`. **Your machine**: also `-frontend`, `-ingest`, `-eval` (below) | `docs/prelaunch_evidence.json`, `security_scan` |
| **bandit** | the application's own code for risky patterns | CI job `bandit` (alert only), and on your machine | the baseline `.bandit-baseline.json` |
| **gitleaks** | secrets in the repository history | CI (the secret-scan job) and Run 24 before a push | Run 24 |

## The packages CI does not audit (Run 36)

CI audits the runtime and development files. The other three belong to the admin machine or the
frontend service, so audit them on your machine and record the result:

    uvx --from pip-audit==2.10.1 pip-audit -r requirements-frontend.txt
    uvx --from pip-audit==2.10.1 pip-audit -r requirements-ingest.txt
    uvx --from pip-audit==2.10.1 pip-audit -r requirements-eval.txt

The two advisories on RAGAS and its cache library are accepted (spec 15.7: the eval file is
installed on the admin machine only); an advisory in the ingest or eval file is **open** until
you decide, and an open critical or high finding keeps this gate item at FAIL.

## bandit and the accepted-findings baseline

`.bandit-baseline.json` is the list of findings the owner accepted on 2026-10-13 (Phase 16). It
holds, for each finding, the file, the line, the rule, the severity, the confidence, bandit's text
and **the reason it is accepted** (`accepted_because`). **No code is quoted**: the `code` key is
there only because bandit's loader requires it, and it is empty.

CI runs bandit and `scripts/bandit_check.py` compares the scan with the baseline, so only a
**new** finding turns the job red. The comparison is by (file, rule, severity, confidence, text),
counted, with the file name normalised to forward slashes, so it works the same on your Windows
machine and on CI's Linux. Rebuild the baseline deliberately, never to silence a new finding:

    uvx bandit==1.9.4 -r app scripts load frontend -q -f json -o bandit_raw.json
    uv run python scripts/bandit_baseline.py bandit_raw.json

The script stops on a finding of a rule that has no written reason, and it never accepts a HIGH
finding. Delete `bandit_raw.json` afterwards (it quotes code).

What is in it, and why each kind is accepted:

| Rule | What bandit says | Why it is accepted |
|---|---|---|
| B608 (hardcoded SQL expressions) | a query is built with an f-string | the only parts put into the text are table and column names and lists of `?` placeholders written by our code; every value is a bound parameter. A value from a request never reaches the SQL text |
| B110 (try/except/pass) | an error is swallowed | each is a best-effort clean-up or a "never break the request for a metric" path, where the failure is reported by an event or an alert elsewhere |
| B101 (assert) | `assert` is used | internal consistency checks in admin-machine tools, not security checks |
| B105 (hardcoded password string) | a string looks like a password | names of settings, labels and field names (`"access_token"` as a JSON key, `"LANGFUSE_PUBLIC_KEY"` as a variable name), never a value |
| B311 (pseudo-random) | `random` is used | load-test question choice and a retry wait; nothing secret depends on it |
| B603, B607, B404 (subprocess) | a program is started | `git` with fixed arguments (release records) and the detached evaluation start; no shell, no user text |

The one **high** finding of the first scan was fixed, not accepted: `frontend/render.py` held
literal direction-control characters in a pattern (B613, "Trojan Source"). They are now written as
escapes, and `tests/test_source_hygiene.py` keeps any such character out of the code.

## Open findings (update after each scan)

| Date | Scan | Critical open | High open | Notes |
|---|---|---|---|---|
| | pip-audit (all five files) | | | |
| | bandit against the baseline | | | |
| | gitleaks | | | |
