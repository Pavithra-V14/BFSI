# Release record: {{title}}

- **Date:** {{date}}
- **Kind:** {{kind}} (prompt, model, retrieval, guardrail, code, corpus or config)
- **Commit:** {{commit}} on {{branch}}
- **Evaluation needed:** {{needs_evaluation}}
- **Approved by:** {{approved_by}}

## What changed
Write it in two or three plain sentences: what, and why.

## Files changed (from git, since {{since}})
{{changed}}

## Evaluation result against the baseline
- Run id and the date:
- Golden-set score before and after:
- The variance band (about two standard deviations):
- Inside the band or outside, and which categories moved:
- Guardrail table and the red-team set (pass or fail):

## Risks and what to watch after the release
## How to go back
Render dashboard, the service, Deploys, the last good deploy, Rollback
(`docs/runbooks/bad-deploy-rollback.md`). Corpus changes: `docs/runbooks/document-withdrawn.md`.

## Approval
Name, date, and where the approval was given (a message, a meeting).
