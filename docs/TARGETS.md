# Numeric targets

Spec 14 and CLAUDE.md: numeric quality targets are **"baseline"** until the first baseline
evaluation run. This page lists every target and starting value, the rule that sets it, and where
it stands. Nothing here is a number the project made up: a target stays "baseline" until the run
that measures it has happened and the owner has decided.

## How a target is set (owner decision, Phase 16)

There are four kinds, and each target below says which one it is:

| Rule | What it means | Alert when |
|---|---|---|
| **regression floor** | baseline mean **minus the variance band** (about two standard deviations of three unchanged runs of the active set). A score inside the band is noise | a score falls below it |
| **ceiling** | latency p95 and tokens (cost) per query: **baseline plus 20 %** | the value rises above it |
| **absolute floor** | for the safety-critical measures (citation validity, faithfulness, refusal correctness, access isolation) a number **the owner signs off, independent of the baseline**. The effective floor is the higher of this and the regression floor | the value is below it |
| **zero rule** | "should be zero" rates (PII leak, cross-role leak, prompt leak, stale answer): target **0** | **any** non-zero value |

All of it is alerts only (build rule 5): no target blocks a merge or a release.

`uv run python -m app.eval targets` (add `--production` for the cloud database) reads the three
baseline runs and prints, for each target below, the baseline, the band, the signed floor and the
**proposed** target. It writes nothing. A target stays "baseline", with the reason printed, when
a metric was measured in fewer than all three runs (`measured in N of 3 runs: no proposal`),
when the band is larger than the mean (`no usable target (band larger than the mean)`: a floor
below zero could never alert, so it is not clamped to a made-up number) or when a ceiling's
baseline is 0. The owner records each decision in the table below; an
absolute floor is signed by adding it, with name and date, to
`app/eval/data/targets_signed.json` (a number outside 0 to 1, or without a name and an ISO
date `YYYY-MM-DD`, is ignored).

## A. Targets the baseline sets (from the evaluation runs)

| Target | Rule | Measured from | Spec | Status | Signed floor / decision |
|---|---|---|---|---|---|
| golden score | regression floor | `case_pass`, the three variance runs | 15.4, 15.4a | baseline | |
| fact recall | regression floor | `fact_recall` | 15.4a | baseline | |
| source hit rate | regression floor | `source_hit` | 15.4a | baseline | |
| context precision | regression floor | `context_precision_anchor` | 15.4a | baseline | |
| answer relevancy | regression floor | `answer_relevancy` (judge) | 15.4a | baseline | |
| latency p95 | ceiling (+20 %) | item `latency_ms` | 15.4a, 14 | baseline | |
| tokens per query | ceiling (+20 %) | item `tokens` (the cost per query) | 15.4a, 18 | baseline | |
| citation validity | absolute floor | `citation_accuracy_code` | 15.4a | baseline | to be signed by the owner |
| numeric faithfulness | absolute floor | `numeric_faithfulness` | 15.4a | baseline | to be signed by the owner |
| faithfulness | absolute floor | `faithfulness` (judge) | 15.4a | baseline | to be signed by the owner |
| refusal correctness | absolute floor | `refusal_correct_code` | 15.4a | baseline | to be signed by the owner |
| access isolation | absolute floor | role-isolation cases with nothing of the staff document in the answer | 13.1, 15.2 | baseline | to be signed by the owner |
| PII leak rate | zero rule | `pii_leak` | 15.4a | **0** | any non-zero is an alert |
| cross-role leak rate | zero rule | role-isolation cases that leaked | 15.4a | **0** | any non-zero is an alert |
| prompt leak rate | zero rule | `prompt_leak` | 9 | **0** | any non-zero is an alert |
| stale answer rate | zero rule | source-removed and edit cases that leaked | 10, 15.4a | **0** | any non-zero is an alert |

## B. Red-team set

- About 40 cases in `app/eval/data/redteam.json`, separate from the 60-case set (which is not
  touched). **20 development** (`rt-dev-*`) and **20 held-out** (`rt-held-*`), each half with
  injection, role-isolation, out-of-domain and PII cases. No real data.
- **The gate is judged on the held-out half only.** The development half is for finding
  weaknesses and fixing them; fixing against it never raises the gated number.
- Its own estimate and code: `uv run python -m app.eval estimate --set redteam_heldout`
  (or `redteam_dev`). Its dataset version (`redteam-1`) keeps it out of the golden-score series
  and out of the baseline.
- The pass threshold for the held-out half is **the owner's number**, recorded in
  `docs/prelaunch_evidence.json` (`red_team.threshold`); the gate does not invent one. Any
  PII leak or cross-role leak in either half is a zero-rule alert whatever the pass rate.
- A fix to the system and a change to a red-team case are never made in the same week (spec 15.1).
- **Held-out results are shown as a count only**: the admin page hides the ids of failed
  `rt-held-` cases (development ids are shown), and the run instructions ask for the count.
  Looking at which held-out case failed and then fixing for it turns that half into a second
  development half; if that happens, say so and the half is replaced with new cases.
- **A limit of the no-shared-question rule**: questions are compared after lower-casing and
  folding punctuation and spaces. A reworded near-duplicate (one word added, a synonym) is
  not caught by the code; the second review reads the two halves for it.

## C. Starting values to confirm or replace at the baseline (the Phase 16 list)

Each is a placeholder [T] today. The baseline run is where it is measured; the owner decides the
final value. Until then the current value stands and the status is "baseline".

| Value | Current | Where | What settles it |
|---|---|---|---|
| out-of-domain probability to refuse | 0.60 | spec 4.5 / 4.1 | Run 21 out-of-domain and vague cases |
| needs-history and vague flags | 0.50 | spec 4.5 | multi-turn and vague cases |
| context guard relevance floor | 0.30 | spec 9 | context precision and recall |
| retrieval confidence floors | 0.40 low confidence, 0.50 | spec 4.5, 7 | answered-correctly rate by score |
| soft filter confidence | 0.80 | spec 4.5 | soft-filter accuracy and fallback rate |
| node timeouts, request budget | 30 s total | spec 4, 14 | Run 27 real run (the owner approves it) |
| 3,400-token budget estimate | 3,400 | spec 4.5 | measured tokens per query |
| memory buffer size | 400 (sweep 200, 400, 800, 1,200) | spec 8 | multi-turn set |
| summary 150 tokens, 80 and 1,500 limits | 150 / 80 / 1,500 | spec 8 | summary fidelity |
| per-user daily token budget | customer 50,000; **staff 200,000** (owner, Phase 16) | spec 5.4, 23 | production usage |
| shadow period | **two weeks** (owner, Phase 16) | spec 19, 23 | the stage checklist |
| review-queue overdue limit | **2 working days** (owner, Phase 16) | spec 11.4, 23 | review queue age |
| alert thresholds, daily webhook cap | see `.env.example` | spec 14 | a week of shadow traffic |
| audit retention | 30 days | spec 12.1 | the owner's retention decision |
| chat retention | 90 days | spec 8.4 | the owner's retention decision |
| sample rate and sample retention | 5 %, 30 days | spec 15.4 | the cost of judging |
| calibration agreement for Jev | 0.85 | spec 15.3 | Run 20 |
| estimate margin | 25 % | spec 15 | measured per-call sizes |
| per-call sizes of the estimator | placeholders | `app/eval/estimate.py` | measured tokens and requests |
| PSI alert level, cost creep | 0.25, 30 % | spec 15.5a | a few weekly drift reports |
| load-test steps and pass rules | 10 to 160 users; 1 % failed, 30 s p95, 80 % active | spec 16 | Run 26 and 27 |
| coverage target | 80 % on four packages | spec 16 | the coverage job |
| queries per day, monthly budget ceiling | **to be named by the owner** | spec 18, 23 | the owner |
| named business owner | **to be named by the owner** | spec 15.6, 17.6 | the owner |

## D. Measured from the dashboards after launch (not set by the baseline)

Uptime, error rate, MTTD and MTTR, cache hit rate (exact and semantic), wrong-hit and stale-answer
incidents (target 0), failover rates, billed against shadow cost, budget variance, deflection
(business metric, 15.6). They get their targets from the first weeks of shadow and staff-beta
traffic, by the same four rules.
