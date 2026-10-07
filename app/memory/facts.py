"""Session facts (spec 8.3): structured state, filled WITHOUT an extra model call [P].

`sessions.session_facts` is JSON text. Keys:
- `product_line`, `topic`: short labels from the Jev triage output (the closed value sets);
- `entities`: approved entity names found in the masked question by deterministic matching
  against the approved allow-list terms (only names that survived masking can match);
- `unclear_turns`: the count of consecutive unclear turns (see `unclear.py`).

`unclear_turns` is bookkeeping and is NEVER given to a model: whatever goes into a prompt passes
through `facts_for_prompt`, which keeps only the first three keys.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping
from typing import Any

from app.db.connection import Database
from app.guardrails.injection import scan_chunk
from app.guardrails.normalise import normalise_entity
from app.memory.store import owned_session, parse_facts

PROMPT_KEYS = ("product_line", "topic", "entities")  # the only keys a prompt may carry
MAX_ENTITIES = 20
MAX_LABEL = 60
_LABEL_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 &/_.,'()-]*$")


def clean_label(value: Any) -> str | None:
    """A short plain label, or None when it is not one (wrong type, too long, odd characters, or
    it reads like an instruction). Triage output is untrusted until it passes this."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > MAX_LABEL or not _LABEL_OK.match(text) or scan_chunk(text):
        return None
    return text


def match_entities(masked_text: str, allow_terms: Iterable[str]) -> list[str]:
    """Approved terms (already normalised) that occur in the masked question as whole words."""

    def words(text: str) -> str:  # the same simplification on both sides: punctuation is a space
        return " ".join(re.sub(r"[^\w@'-]+", " ", normalise_entity(text)).split())

    haystack = f" {words(masked_text)} "
    found = []
    for term in sorted(set(allow_terms)):
        if term and len(term) <= MAX_LABEL and f" {words(term)} " in haystack:
            found.append(term)
    return found


def facts_for_prompt(facts: Mapping[str, Any]) -> dict[str, Any]:
    """The facts a prompt may carry. Bookkeeping such as `unclear_turns` is dropped here."""
    return {key: facts[key] for key in PROMPT_KEYS if facts.get(key)}


def render_facts(facts: Mapping[str, Any]) -> str:
    """One line for the rewrite prompt, from `facts_for_prompt` only."""
    shown = facts_for_prompt(facts)
    parts = []
    if "product_line" in shown:
        parts.append(f"product line: {shown['product_line']}")
    if "topic" in shown:
        parts.append(f"topic: {shown['topic']}")
    if "entities" in shown:
        parts.append("names: " + ", ".join(shown["entities"]))
    return "; ".join(parts)


def update_facts(
    db: Database,
    user_id: str,
    session_id: str,
    *,
    product_line: Any = None,
    topic: Any = None,
    entities: Collection[str] = (),
    allowed_product_lines: Collection[str] | None = None,
) -> dict[str, Any]:
    """Merge new facts into the session's JSON and return the result. A value that is not a clean
    label is ignored (the old one stays); a product line outside `allowed_product_lines`, when
    given, is ignored too. Entities are added (newest kept, at most 20)."""
    line = clean_label(product_line)
    if line is not None and allowed_product_lines is not None and line not in allowed_product_lines:
        line = None
    topic_label = clean_label(topic)
    with db.transaction(write=True):
        row = owned_session(db, user_id, session_id, lock=True)
        facts = parse_facts(row[7])
        if line is not None:
            facts["product_line"] = line
        if topic_label is not None:
            facts["topic"] = topic_label
        known = [e for e in facts.get("entities", []) if isinstance(e, str)]
        for entity in entities:
            if isinstance(entity, str) and entity and entity not in known:
                known.append(entity)
        if known:
            facts["entities"] = known[-MAX_ENTITIES:]
        db.execute(
            "UPDATE sessions SET session_facts = ? WHERE id = ? AND user_id = ?",
            (json.dumps(facts, sort_keys=True), session_id, user_id),
        )
    return facts
