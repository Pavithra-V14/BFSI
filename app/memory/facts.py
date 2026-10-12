"""Session facts (spec 8.3): structured state, filled WITHOUT an extra model call [P].

`sessions.session_facts` is JSON text. Keys:
- `product_line`, `topic`: short labels from the Jev triage output (the closed value sets);
- `entities`: approved entity names found in the masked question by deterministic matching against
  the approved allow-list, each stored with the ids of the documents that approve it
  (`{"name": "irdai", "doc_ids": ["doc-a"]}`), so that deleting a document removes the entities that
  only it approved (`documents.clear_memory_for_document`);
- `unclear_turns`: the count of consecutive unclear turns (see `unclear.py`).

Everything is validated when it is WRITTEN and again when it is READ for a prompt, because the JSON
column can be changed by any code: a label must be a short plain label that does not read like an
instruction, an entity name must be a clean label with at least one document id.

`unclear_turns` is bookkeeping and is NEVER given to a model: whatever goes into a prompt passes
through `facts_for_prompt`, which keeps only the first three keys, validated and neutralised.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.db.connection import Database
from app.guardrails.allowlist import AllowList
from app.guardrails.context import neutralise
from app.guardrails.injection import scan_chunk
from app.guardrails.normalise import normalise_entity
from app.memory.store import owned_session, parse_facts

PROMPT_KEYS = ("product_line", "topic", "entities")  # the only keys a prompt may carry
MAX_ENTITIES = 20
MAX_LABEL = 60
MAX_DOC_ID = 200
_LABEL_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 &/_.,'()-]*$")


@dataclass(frozen=True)
class Entity:
    """An approved name found in a question and the documents that approve it."""

    name: str
    doc_ids: tuple[str, ...]


def clean_label(value: Any) -> str | None:
    """A short plain label, or None when it is not one (wrong type, too long, odd characters, or
    it reads like an instruction). Triage output is untrusted until it passes this."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > MAX_LABEL or not _LABEL_OK.match(text) or scan_chunk(text):
        return None
    return text


def _clean_doc_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    ids = {d for d in value if isinstance(d, str) and 0 < len(d) <= MAX_DOC_ID and d.isprintable()}
    return tuple(sorted(ids))


def clean_entities(raw: Any) -> list[dict[str, Any]]:
    """The valid entities of a stored `entities` value, as {"name", "doc_ids"} (others dropped)."""
    out: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        name, doc_ids = clean_label(item.get("name")), _clean_doc_ids(item.get("doc_ids"))
        if name and doc_ids:
            out.append({"name": name, "doc_ids": list(doc_ids)})
    return out[-MAX_ENTITIES:]


def match_entities(masked_text: str, allow: AllowList) -> list[Entity]:
    """Approved terms that occur in the masked question as whole words, with the documents that
    approve each."""

    def words(text: str) -> str:  # the same simplification on both sides: punctuation is a space
        return " ".join(re.sub(r"[^\w@'-]+", " ", normalise_entity(text)).split())

    haystack = f" {words(masked_text)} "
    found = []
    for term, doc_ids in sorted(allow.terms_by_doc().items()):
        if term and len(term) <= MAX_LABEL and f" {words(term)} " in haystack:
            found.append(Entity(term, doc_ids))
    return found


def facts_for_prompt(facts: Mapping[str, Any]) -> dict[str, Any]:
    """The facts a prompt may carry, validated again. Bookkeeping such as `unclear_turns`, the
    document ids and anything that is not a clean label are dropped."""
    shown: dict[str, Any] = {}
    for field in ("product_line", "topic"):
        label = clean_label(facts.get(field))
        if label:
            shown[field] = label
    names = [e["name"] for e in clean_entities(facts.get("entities"))]
    if names:
        shown["entities"] = names
    return shown


def render_facts(facts: Mapping[str, Any]) -> str:
    """One line for the rewrite prompt, from `facts_for_prompt` only, neutralised."""
    shown = facts_for_prompt(facts)
    parts = []
    if "product_line" in shown:
        parts.append(f"product line: {shown['product_line']}")
    if "topic" in shown:
        parts.append(f"topic: {shown['topic']}")
    if "entities" in shown:
        parts.append("names: " + ", ".join(shown["entities"]))
    return neutralise("; ".join(parts))


def update_facts(
    db: Database,
    user_id: str,
    session_id: str,
    *,
    product_line: Any = None,
    topic: Any = None,
    entities: Iterable[Entity] = (),
    allowed_product_lines: Collection[str] | None = None,
) -> dict[str, Any]:
    """Merge new facts into the session's JSON and return the result. A value that is not a clean
    label is ignored (the old one stays); a product line outside `allowed_product_lines`, when
    given, is ignored too. Entities are added by name (their document ids are joined); an entity
    that is not valid is ignored; the newest 20 are kept. A closed session refuses (the same
    error as a missing one)."""
    line = clean_label(product_line)
    if line is not None and allowed_product_lines is not None and line not in allowed_product_lines:
        line = None
    topic_label = clean_label(topic)
    with db.transaction(write=True):
        row = owned_session(db, user_id, session_id, lock=True, writable=True)
        facts = parse_facts(row[7])
        if line is not None:
            facts["product_line"] = line
        if topic_label is not None:
            facts["topic"] = topic_label
        known = {e["name"]: e for e in clean_entities(facts.get("entities"))}
        for entity in entities:
            if not isinstance(entity, Entity):
                continue  # only what match_entities made (or an Entity built to the same rules)
            name, doc_ids = clean_label(entity.name), _clean_doc_ids(list(entity.doc_ids))
            if not name or not doc_ids:
                continue
            merged = set(known.pop(name, {}).get("doc_ids", [])) | set(doc_ids)
            known[name] = {"name": name, "doc_ids": sorted(merged)}  # newest last
        kept = list(known.values())[-MAX_ENTITIES:]
        if kept:
            facts["entities"] = kept
        else:
            facts.pop("entities", None)
        db.execute(
            "UPDATE sessions SET session_facts = ? WHERE id = ? AND user_id = ?",
            (json.dumps(facts, sort_keys=True), session_id, user_id),
        )
    return facts
