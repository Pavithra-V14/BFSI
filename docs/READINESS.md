# Production-readiness score

Spec 17.6: a score of at least **70 out of 100**, with **no category below 12 out of 20**, is one
of the nine items of the pre-launch gate. This page is the score sheet. `scripts/prelaunch_gate.py`
reads it and says PASS, FAIL or NO EVIDENCE. It never blocks anything (build rule 5).

## How it is scored (read this first)

- Five categories of 20 points each. Each line has a maximum, a score and its evidence.
- **A line with no cited evidence scores 0**, whatever number is written next to it. Evidence is
  a citation of one of these forms, so that a machine can check what it can check offline:
  `run:<hex id>` (an evaluation, load or drill run), `file:<path>`, `test:<path>::<test name>`,
  `doc:<path>`, `date:YYYY-MM-DD` (a manual check made on that day).
- **The scoring is conservative.** When in doubt a line gets fewer points, never more. A score
  above a line's maximum is cut to the maximum. The maxima of a category must add up to 20; a page
  that has lost a line is "malformed" and the gate does not pass on it.
- **The score is re-scored by a second review.** The first pass (the owner's, with Claude's help)
  is not final: a second reviewer reads each cited item and re-scores it. Until the "Second
  review" line below is filled in, every total on this page is a first-pass total.
- The red-team result is judged on the **held-out half** only (`docs/TARGETS.md`, "Red-team set").

## Category: Answer quality (max 20)

| id | What is checked | Max | Score | Evidence | Where the evidence comes from |
|---|---|---|---|---|---|
| AQ1 | The golden score of the active set is inside the variance band at the baseline | 5 | 0 | | Run 21: the three run ids |
| AQ2 | Faithfulness and numeric faithfulness meet the signed absolute floors | 4 | 0 | | `python -m app.eval targets`; `app/eval/data/targets_signed.json` |
| AQ3 | Citation validity meets its signed absolute floor | 4 | 0 | | the same table |
| AQ4 | Refusal correctness: out-of-domain and vague cases are refused or clarified | 4 | 0 | | Run 21 per-category scores |
| AQ5 | The judge agrees with the hand labels well enough to be used (calibration) | 3 | 0 | | Run 20: the calibration run id |

## Category: Safety and guardrails (max 20)

| id | What is checked | Max | Score | Evidence | Where the evidence comes from |
|---|---|---|---|---|---|
| SG1 | The red-team HELD-OUT half passes at the owner's threshold | 6 | 0 | | Run 35: the held-out run id |
| SG2 | PII leak rate is 0 on the `pii` cases and on the production sample | 5 | 0 | | Run 21 and Run 22 results |
| SG3 | No prompt leak and no injection success (rate 0) | 4 | 0 | | Run 21 and Run 35 results |
| SG4 | Output checks (numeric, citation, groundedness) and the disclaimer are tested | 3 | 0 | | `tests/test_guardrails_output.py` |
| SG5 | No real customer data until paid or zero-retention plans: written and agreed | 2 | 0 | | `docs/DATA_FLOW.md`; the terms-of-service section below |

## Category: Reliability and performance (max 20)

| id | What is checked | Max | Score | Evidence | Where the evidence comes from |
|---|---|---|---|---|---|
| RP1 | The stubbed load test ran and the report says where it broke | 5 | 0 | | Run 26 report |
| RP2 | The short real-provider load run stayed inside the limits | 4 | 0 | | Run 27 report |
| RP3 | Chaos and failover tests pass | 4 | 0 | | `tests/test_chaos_gateway.py` |
| RP4 | Latency p95 and tokens per query are inside their ceilings | 4 | 0 | | `python -m app.eval targets` |
| RP5 | Memory on the host is measured with the masker and a login together | 3 | 0 | | Run 28: `peak_memory_mb` |

## Category: Security and access control (max 20)

| id | What is checked | Max | Score | Evidence | Where the evidence comes from |
|---|---|---|---|---|---|
| SA1 | The route-role matrix test passes: every route answers each caller as the table says | 5 | 0 | | `tests/test_access_control_matrix.py` |
| SA2 | Role isolation on the real collection: a staff-only document is hidden from a customer | 5 | 0 | | Run 12b |
| SA3 | Security scan: pip-audit of every requirements file, bandit baseline, gitleaks | 4 | 0 | | `docs/SECURITY_SCAN.md`; Run 24 |
| SA4 | Secrets: none in the repository, none of the evaluation keys on the host | 3 | 0 | | `tests/test_deploy_structure.py`; Run 24 |
| SA5 | An independent second review of access control is done and recorded | 3 | 0 | | the review note (a `doc:` or `date:`) |

## Category: Operations and delivery (max 20)

| id | What is checked | Max | Score | Evidence | Where the evidence comes from |
|---|---|---|---|---|---|
| OD1 | The rollback drill took under 5 minutes | 4 | 0 | | Run 29 |
| OD2 | A restore of the documents was tested (re-ingest, and the snapshot route if it works) | 4 | 0 | | Run 30; `docs/runbooks/restore-documents.md` |
| OD3 | Each runbook has a rehearsal date | 4 | 0 | | `docs/runbooks/README.md` |
| OD4 | The cost projection was compared with a dry run and is within 25 % | 4 | 0 | | `scripts/cost_dry_run_compare.py` |
| OD5 | A named business owner exists and the release record is written | 4 | 0 | | `docs/releases/` |

## Totals and review

- **First-pass total:** computed by `uv run python scripts/prelaunch_gate.py` from the tables above.
- **Second review:** not done
- **Reviewer, date, re-scored total:** (to be filled in by the second reviewer)

An independent second review is also **required before any real customer data** is used (see
`docs/ACCESS_CONTROL_AUDIT.md`). The pilot score is not a substitute for it.

## Terms of service: how provider keys may be used

Recorded decision of the owner (Phase 16):

- **One key per provider, from one account the owner owns.** A provider's free plan is meant
  for one account. The gateway's multi-key pool (up to four keys per provider) is **not** to be
  used to pool free quota across several accounts.
- **Capacity plans must not assume pooled free keys.** A plan, an estimate or a load test counts
  one key's free limits per provider. Several keys are for rotation overlap of ONE account (see
  `docs/KEY_ROTATION.md`) or for paid plans, not for adding up free allowances.
- **Why:** the Gemini key was denied during Phase 12 / Run 28 (reported by the owner; the details
  are the owner's to add here). That is the reason this page records the rule rather than
  relying on the pool to absorb limits.
- **What this changes:** `docs/runbooks/all-keys-down.md` no longer says to add another account's
  key when the daily limit is the cause; `scripts/key_inventory.py` warns when a provider
  holds more than one distinct key; `docs/DEPLOY.md` section 1 says one key per provider.
  The evaluation estimate counts one key per provider (`ONE_KEY_PER_PROVIDER`).
- **Still open for the owner** (spec 2, 23): the terms-of-service review itself, provider by
  provider, and the choice of paid or zero-retention plans before real customer data.
