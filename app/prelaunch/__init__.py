"""The pre-launch gate and the stage checklists (spec 17.6, 19; Phase 16).

Admin-machine tooling: it reads files of this repository (the readiness page, the evidence file)
and prints a report. Alert only (build rule 5): nothing here blocks a merge, a deploy or a stage
change; the report is attached to the release record. Never imported by the hosted API.
"""
