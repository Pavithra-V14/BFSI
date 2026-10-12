# Runbooks

One page each, with exact steps (spec 17.5). Rehearse each one, then write the date here. A
runbook that has never been rehearsed is a guess.

| Runbook | When | Last rehearsal (date, who, minutes) |
|---|---|---|
| [All keys down](all-keys-down.md) | answers say "temporarily unavailable" | never |
| [Wrong answer reported](wrong-answer-reported.md) | a user or you found a wrong answer | never |
| [Document withdrawn](document-withdrawn.md) | a document must stop being shown | never |
| [Suspected personal data](suspected-pii-leak.md) | a name or number where it must not be | never |
| [Key leaked](key-leaked.md) | a key or secret was exposed | never |
| [Bad deploy: go back](bad-deploy-rollback.md) | the service is broken after a deploy | never |
| [Restore the documents](restore-documents.md) | the collection is damaged or empty | never |

The rollback target is **under 5 minutes** (spec 17.3). The drill is Run 29 in
`docs/OWNER_RUNS.md`.
