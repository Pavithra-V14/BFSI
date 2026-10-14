"""The pre-launch gate report (spec 17.6) and the stage checklists (spec 19). ALERT ONLY.

    uv run python scripts/prelaunch_gate.py                          # the nine items
    uv run python scripts/prelaunch_gate.py --stage staff_beta       # a stage's checklist
    uv run python scripts/prelaunch_gate.py --write                  # also save the report

It reads `docs/READINESS.md` and `docs/prelaunch_evidence.json` (what you recorded after each
run) and prints PASS, FAIL or NO EVIDENCE for each item with the reason. It exits 0 whatever it
says: no gate blocks a merge, a deploy or a stage change (build rule 5). `--write` saves the
report as `docs/releases/YYYY-MM-DD-prelaunch-gate.md` (never overwriting a file), to be attached
to the release record. It calls no service and reads no secret.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(
    argv: list[str] | None = None,
    *,
    out: Callable[[str], None] = print,
    evidence: dict | None = None,
    folder: Path | None = None,
    now: datetime | None = None,
) -> int:
    from app.prelaunch import gate

    parser = argparse.ArgumentParser(description="pre-launch gate report (alert only)")
    parser.add_argument("--stage", choices=sorted(gate.STAGES), help="a stage checklist instead")
    parser.add_argument("--write", action="store_true", help="save the report in docs/releases/")
    args = parser.parse_args(argv)
    if args.stage:
        verdicts = gate.stage_checklist(args.stage, evidence)
        title = f"Stage checklist: {args.stage}"
        slug = f"stage-{args.stage}"
    else:
        verdicts = gate.gate(evidence)
        title = "Pre-launch gate (spec 17.6)"
        slug = "prelaunch-gate"
    out(gate.render(verdicts, title).rstrip("\n"))
    if args.write:
        moment = now or datetime.now(UTC)
        target = (folder or ROOT / "docs" / "releases") / f"{moment:%Y-%m-%d}-{slug}.md"
        if target.exists():
            out(f"already there: {target.name}; nothing was overwritten")
            return 0
        target.write_text(gate.render(verdicts, title, markdown=True), "utf-8")
        out(f"saved {target.name}: attach it to the release record")
    return 0


if __name__ == "__main__":
    sys.exit(main())
