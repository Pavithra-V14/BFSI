"""Make a release record from the template (spec 17.3).

    uv run python scripts/release_record.py --title "Tighten the refusal wording" \
        --kind prompt --approved-by "the admin"

It copies `docs/releases/TEMPLATE.md` to `docs/releases/YYYY-MM-DD-<slug>.md` and fills in what git
knows: the commit, the branch, the files changed since the last record's commit (names only). The
evaluation result against the baseline and the approval are for the person to write: a prompt,
model, retrieval or guardrail change always gets a record. It reads no secret and calls no
service.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASES = ROOT / "docs" / "releases"
KINDS = ("prompt", "model", "retrieval", "guardrail", "code", "corpus", "config")
ALWAYS_RECORDED = ("prompt", "model", "retrieval", "guardrail")


def _git(*args: str) -> str:
    try:
        done = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "release"


def render(template: str, **values: str) -> str:
    text = template
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def main(
    argv: list[str] | None = None,
    *,
    out: Callable[[str], None] = print,
    folder: Path | None = None,
    now: datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="make a release record")
    parser.add_argument("--title", required=True)
    parser.add_argument("--kind", choices=KINDS, required=True)
    parser.add_argument("--approved-by", default="")
    args = parser.parse_args(argv)
    target = folder or RELEASES
    template_path = target / "TEMPLATE.md"
    if not template_path.is_file():
        out("no template: docs/releases/TEMPLATE.md is missing")
        return 1
    moment = now or datetime.now(UTC)
    commit = _git("rev-parse", "--short", "HEAD") or "unknown"
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    changed = _git("diff", "--name-only", "HEAD~1", "HEAD").splitlines()[:40]
    path = target / f"{moment.strftime('%Y-%m-%d')}-{slug(args.title)}.md"
    if path.exists():
        out(f"already there: {path.name}; nothing was overwritten")
        return 1
    text = render(
        template_path.read_text("utf-8"),
        title=args.title,
        date=moment.strftime("%Y-%m-%d"),
        kind=args.kind,
        commit=commit,
        branch=branch,
        changed="\n".join(f"- {name}" for name in changed) or "- (none found)",
        approved_by=args.approved_by or "(to be filled in)",
        needs_evaluation="yes: always"
        if args.kind in ALWAYS_RECORDED
        else "if it can change answers",
    )
    path.write_text(text, "utf-8")
    out(f"made {path.name}: fill in the evaluation result and the approval, then commit it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
