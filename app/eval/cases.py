"""The evaluation set: cases, calibration items, and the rules a set must obey (spec 15.2).

A case is a question (or a short conversation) written by the project from the sample corpus, the
role that asks it, what the system should do, and where the answer lives (by clause reference and
an anchor phrase, never by chunk id). The rules are code: a set that breaks one does not load.

- 60 cases, 20 of them active, at least one active case in every category (spec 15.2);
- only documents of the corpus manifest are named (owner addition c: no real customer data);
- no text that looks like a real person's data, except in the `pii` category, whose cases hold
  made-up identifiers on purpose;
- 30 calibration items, hand-labelled, of which 10 are the expert-review sample [T].
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.eval import constants as c
from app.eval.corpus import DATA, load_manifest, unknown_sources
from app.observability.scan import scan_text

CASES_PATH = DATA / "cases.json"
CALIBRATION_PATH = DATA / "calibration.json"

OPS = ("withdraw_document", "edit_text", "supersede_text")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")
MAX_TURN_CHARS = 500
CALIBRATION_METRICS = (
    "claim_supported",
    "refusal_correct",
    "chunk_relevant",
    "citation_supports",
    "injection_call_right",
)


@dataclass(frozen=True)
class Source:
    doc_id: str
    clause_ref: str
    anchor: str  # a phrase of the expected clause: how a retrieved chunk is recognised


@dataclass(frozen=True)
class Op:
    """A change made to the TEST collection before a turn, undone after the case."""

    op: str
    doc_id: str
    before_turn: int = 0
    anchor: str = ""  # a phrase that finds the chunk to edit
    old: str = ""
    new: str = ""


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    role: str
    turns: tuple[str, ...]
    behaviour: str
    sources: tuple[Source, ...] = ()
    facts: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    reference: str = ""
    setup: tuple[Op, ...] = ()
    source_docs: tuple[str, ...] = ()
    active: bool = False
    status: str = "set"
    origin: str = "authored"
    review_item_id: str | None = None
    dataset_version: str = ""
    review: bool = False  # [T] one of the cases the owner must read (all active cases)

    @property
    def question(self) -> str:
        return self.turns[-1]


@dataclass(frozen=True)
class CalibrationItem:
    id: str
    metric: str
    question: str
    context: str
    answer: str
    label: bool  # the hand label: True means the verdict question's answer is "yes"
    why: str
    review: bool = False  # in the expert-review sample [T]


def _strings(value: Any, what: str) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise c.EvalConfigError(f"{what} must be a list of non-empty text")
    return tuple(v.strip() for v in value)


def case_from_dict(data: dict[str, Any], dataset_version: str = "") -> Case:
    try:
        expect = data.get("expect") or {}
        sources = tuple(
            Source(str(s["doc_id"]), str(s.get("clause_ref", "")), str(s.get("anchor", "")))
            for s in (expect.get("sources") or [])
        )
        setup = tuple(
            Op(
                op=str(o["op"]),
                doc_id=str(o["doc_id"]),
                before_turn=int(o.get("before_turn", 0)),
                anchor=str(o.get("anchor", "")),
                old=str(o.get("old", "")),
                new=str(o.get("new", "")),
            )
            for o in (data.get("setup") or [])
        )
        return Case(
            id=str(data["id"]),
            category=str(data["category"]),
            role=str(data["role"]),
            turns=_strings(data.get("turns"), "turns"),
            behaviour=str(expect.get("behaviour", "")),
            sources=sources,
            facts=_strings(expect.get("facts"), "facts"),
            forbidden=_strings(expect.get("forbidden"), "forbidden"),
            reference=str(expect.get("reference", "")),
            setup=setup,
            source_docs=_strings(data.get("source_docs"), "source_docs"),
            active=bool(data.get("active", False)),
            status=str(data.get("status", "set")),
            origin=str(data.get("origin", "authored")),
            review_item_id=data.get("review_item_id"),
            dataset_version=str(data.get("dataset_version") or dataset_version),
            review=bool(data.get("review", False)),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise c.EvalConfigError(f"a case is malformed ({type(error).__name__})") from None


def case_to_dict(case: Case) -> dict[str, Any]:
    return {
        "id": case.id,
        "category": case.category,
        "role": case.role,
        "active": case.active,
        "status": case.status,
        "origin": case.origin,
        "review_item_id": case.review_item_id,
        "dataset_version": case.dataset_version,
        "review": case.review,
        "turns": list(case.turns),
        "expect": {
            "behaviour": case.behaviour,
            "sources": [
                {"doc_id": s.doc_id, "clause_ref": s.clause_ref, "anchor": s.anchor}
                for s in case.sources
            ],
            "facts": list(case.facts),
            "forbidden": list(case.forbidden),
            "reference": case.reference,
        },
        "setup": [
            {
                "op": o.op,
                "doc_id": o.doc_id,
                "before_turn": o.before_turn,
                "anchor": o.anchor,
                "old": o.old,
                "new": o.new,
            }
            for o in case.setup
        ],
        "source_docs": list(case.source_docs),
    }


def case_problems(case: Case, manifest: dict | None = None) -> list[str]:
    """What is wrong with one case (empty list: nothing). Messages name the case and the rule."""
    manifest = manifest or load_manifest()
    bad: list[str] = []
    name = case.id

    def problem(text: str) -> None:
        bad.append(f"{name}: {text}")

    if not ID_PATTERN.match(case.id):
        problem("the id must be lower-case letters, digits, '-' or '_'")
    if case.category not in c.CATEGORIES:
        problem("unknown category")
    if case.role not in c.ROLES:
        problem("unknown role")
    if case.behaviour not in c.BEHAVIOURS:
        problem("unknown expected behaviour")
    if case.status not in c.CASE_STATUSES:
        problem("unknown status")
    if not case.turns:
        problem("no question")
    for turn in case.turns:
        if len(turn) > MAX_TURN_CHARS:
            problem("a question is too long")
    if case.category == "multi_turn" and len(case.turns) < 2:
        problem("a multi-turn case needs at least two turns")
    # Owner addition (c): a case of the SET names only documents of the sample corpus manifest. A
    # candidate (a review-queue item waiting to be curated) may name a live document; it can
    # become a case only when the manifest is extended on purpose.
    unknown = unknown_sources(
        list(case.source_docs) + [s.doc_id for s in case.sources] + [o.doc_id for o in case.setup],
        manifest,
    )
    if unknown and case.status == "set":
        problem("names a document that is not in the sample corpus manifest")
    if case.status == "set":
        listed = set(case.source_docs)
        if any(s.doc_id not in listed for s in case.sources):
            problem("a source is not among source_docs")
        if case.behaviour == "answer":
            if not case.sources:
                problem("an answer case needs an expected source")
            if not case.facts:
                problem("an answer case needs the facts the answer must hold")
            if not case.reference:
                problem("an answer case needs a reference answer")
            if any(not s.anchor for s in case.sources):
                problem("an expected source needs an anchor phrase")
        if case.behaviour != "answer" and case.facts:
            problem("only an answer case lists facts")
    for op in case.setup:
        if op.op not in OPS:
            problem("unknown setup operation")
        if op.op in ("edit_text", "supersede_text") and not (op.anchor and op.old and op.new):
            problem("an edit needs anchor, old and new text")
        if not 0 <= op.before_turn < max(len(case.turns), 1):
            problem("a setup operation names a turn the case does not have")
    # no data that looks like a real person's, apart from the made-up values of the pii category
    if case.category != "pii":
        findings = {f.rule for turn in case.turns for f in scan_text(turn)}
        if findings:
            problem("a question holds text that looks like personal data")
    return bad


def set_problems(cases: list[Case], manifest: dict | None = None) -> list[str]:
    """The rules of the whole set (spec 15.2). Only for a complete set, not for candidates."""
    manifest = manifest or load_manifest()
    bad: list[str] = []
    for case in cases:
        bad.extend(case_problems(case, manifest))
    ids = Counter(case.id for case in cases)
    bad.extend(f"{name}: the id is used twice" for name, n in ids.items() if n > 1)
    members = [case for case in cases if case.status == "set"]
    if len(members) != c.TOTAL_CASES:
        bad.append(f"the set holds {len(members)} cases, not {c.TOTAL_CASES}")
    active = [case for case in members if case.active]
    if len(active) != c.ACTIVE_CASES:
        bad.append(f"{len(active)} cases are active, not {c.ACTIVE_CASES}")
    missing = [cat for cat in c.CATEGORIES if not any(x.category == cat for x in active)]
    if missing:
        bad.append("no active case for: " + ", ".join(missing))
    absent = [cat for cat in c.CATEGORIES if not any(x.category == cat for x in members)]
    if absent:
        bad.append("no case for: " + ", ".join(absent))
    return bad


def load_cases(path: Path = CASES_PATH) -> tuple[str, list[Case]]:
    data = json.loads(path.read_text("utf-8"))
    version = str(data["dataset_version"])
    return version, [case_from_dict(item, version) for item in data["cases"]]


def load_calibration(path: Path = CALIBRATION_PATH) -> list[CalibrationItem]:
    data = json.loads(path.read_text("utf-8"))
    items = []
    for raw in data["items"]:
        try:
            items.append(
                CalibrationItem(
                    id=str(raw["id"]),
                    metric=str(raw["metric"]),
                    question=str(raw["question"]),
                    context=str(raw.get("context", "")),
                    answer=str(raw.get("answer", "")),
                    label=bool(raw["label"]),
                    why=str(raw.get("why", "")),
                    review=bool(raw.get("review", False)),
                )
            )
        except (KeyError, TypeError) as error:
            raise c.EvalConfigError(
                f"a calibration item is malformed ({type(error).__name__})"
            ) from None
    return items


def calibration_problems(items: list[CalibrationItem]) -> list[str]:
    bad: list[str] = []
    if len(items) != c.CALIBRATION_ITEMS:
        bad.append(f"{len(items)} calibration items, not {c.CALIBRATION_ITEMS}")
    ids = Counter(i.id for i in items)
    bad.extend(f"{n}: the id is used twice" for n, k in ids.items() if k > 1)
    for item in items:
        if item.metric not in CALIBRATION_METRICS:
            bad.append(f"{item.id}: unknown metric")
    for metric in CALIBRATION_METRICS:
        mine = [i for i in items if i.metric == metric]
        if mine and not (any(i.label for i in mine) and any(not i.label for i in mine)):
            bad.append(f"{metric}: needs both yes and no labels")
    if sum(1 for i in items if i.review) != c.CALIBRATION_REVIEW_SAMPLE:
        bad.append(f"the expert-review sample must be {c.CALIBRATION_REVIEW_SAMPLE} items")
    return bad
