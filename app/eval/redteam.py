"""The red-team set (spec 17.6 "the red-team set passing"; Phase 16 owner decision).

About 40 adversarial cases in their own file, kept apart from the 60-case evaluation set (which
stays untouched). They are split in two halves of 20:

- **development** (`rt-dev-*`): read, discussed and used to find weaknesses;
- **held-out** (`rt-held-*`): not used to tune anything; the pre-launch gate is judged on this
  half only, so a fix made against the development half is not graded on the same questions.

The rules are code, as for the main set: exact sizes, the two prefixes, ids unique across both
sets, no question text that appears in the main set or in the other half, no case that is
"active" (the 20-case core stays the core), only the categories an attacker uses, only documents
of the sample corpus, and no text that looks like real personal data (the made-up identifiers of
the `pii` category are the one exception, as in the main set). No real data is ever used.

A red-team run uses the same estimate, confirmation code and runner as any evaluation run; its
dataset version (`redteam-1`) gives it its own code, and the dashboards leave it out of the
golden-score series.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.eval.cases import Case, case_problems, load_cases
from app.eval.corpus import DATA, load_manifest

REDTEAM_PATH = DATA / "redteam.json"
VERSION_PREFIX = "redteam"
DEV_PREFIX = "rt-dev-"
HELD_PREFIX = "rt-held-"
HALF = 20
CATEGORIES = ("injection", "role_isolation", "pii", "out_of_domain")
SETS = ("redteam_dev", "redteam_heldout")


def load_redteam(path: Path = REDTEAM_PATH) -> tuple[str, list[Case]]:
    return load_cases(path)


def is_redteam_version(dataset_version: str | None) -> bool:
    return str(dataset_version or "").startswith(VERSION_PREFIX)


def split_of(case: Case) -> str | None:
    """'dev' or 'held' from the id prefix, None for a case that is in neither half."""
    if case.id.startswith(DEV_PREFIX):
        return "dev"
    if case.id.startswith(HELD_PREFIX):
        return "held"
    return None


def select(cases: list[Case], which: str) -> list[Case]:
    """The cases of `redteam_dev` or `redteam_heldout`."""
    half = {"redteam_dev": "dev", "redteam_heldout": "held"}[which]
    return [x for x in cases if split_of(x) == half]


def _normal(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def redteam_problems(cases: list[Case], main_cases: list[Case] | None = None) -> list[str]:
    """What is wrong with the red-team set (empty list: nothing)."""
    manifest = load_manifest()
    if main_cases is None:
        _version, main_cases = load_cases()
    bad: list[str] = []
    dev = [x for x in cases if split_of(x) == "dev"]
    held = [x for x in cases if split_of(x) == "held"]
    if len(dev) != HALF:
        bad.append(f"the development half holds {len(dev)} cases, not {HALF}")
    if len(held) != HALF:
        bad.append(f"the held-out half holds {len(held)} cases, not {HALF}")
    if len(cases) != len(dev) + len(held):
        bad.append("a case is in neither half (its id must start with rt-dev- or rt-held-)")

    seen: dict[str, str] = {}
    main_ids = {x.id for x in main_cases}
    main_text = {_normal(t) for x in main_cases for t in x.turns}
    for case in cases:
        bad.extend(case_problems(case, manifest))
        if case.id in main_ids:
            bad.append(f"{case.id}: the id is used by the main evaluation set")
        if case.category not in CATEGORIES:
            bad.append(f"{case.id}: a red-team case is one of {', '.join(CATEGORIES)}")
        if case.active:
            bad.append(f"{case.id}: a red-team case is never part of the active core")
        if case.setup:
            bad.append(f"{case.id}: a red-team case changes nothing in the test collection")
        if not case.dataset_version.startswith(VERSION_PREFIX):
            bad.append(f"{case.id}: its dataset version must start with {VERSION_PREFIX}")
        for turn in case.turns:
            key = _normal(turn)
            if key in main_text:
                bad.append(f"{case.id}: a question is also in the main evaluation set")
            other = seen.get(key)
            if other is not None and other != case.id:
                bad.append(f"{case.id}: the same question as {other}")
            seen[key] = case.id
    ids: dict[str, int] = {}
    for case in cases:
        ids[case.id] = ids.get(case.id, 0) + 1
    bad.extend(f"{name}: the id is used twice" for name, n in ids.items() if n > 1)
    # both halves must cover every category, or the gate would judge on a lopsided half
    for name, half in (("development", dev), ("held-out", held)):
        missing = [cat for cat in CATEGORIES if not any(x.category == cat for x in half)]
        if missing:
            bad.append(f"the {name} half has no case for: {', '.join(missing)}")
    return bad
