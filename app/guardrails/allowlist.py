"""The approved-entity allow-list as masking sees it (spec 6.8, 9.2).

Ingestion stores one row per variant in `allowlist_entities` (status, doc_id, access_role). Masking
needs the APPROVED terms, normalised with `normalise_entity`, in two scopes:
- `terms(None)`: every approved term the role may see: for the copy sent to models, before
  retrieval;
- `terms(doc_ids)`: only the terms of the documents that survived retrieval: for the copy that is
  stored, logged or cached, so a term approved only in some other document stays masked there.
Which access roles a user's role may see is decided by the caller (Phase 8) and passed in.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Any

from app.guardrails.normalise import normalise_entity


class AllowList:
    def __init__(self, entries: Iterable[tuple[str, str]] = ()) -> None:
        """`entries` are (text, doc_id) pairs; the text is normalised here."""
        self._by_doc: dict[str, set[str]] = {}
        for text, doc_id in entries:
            term = normalise_entity(text)
            if term:
                self._by_doc.setdefault(doc_id, set()).add(term)

    def terms(self, doc_ids: Collection[str] | None = None) -> frozenset[str]:
        if doc_ids is None:
            return frozenset().union(*self._by_doc.values()) if self._by_doc else frozenset()
        wanted = [self._by_doc[d] for d in doc_ids if d in self._by_doc]
        return frozenset().union(*wanted) if wanted else frozenset()

    def terms_by_doc(self) -> dict[str, tuple[str, ...]]:
        """Every approved term with the ids of the documents that approve it (sorted)."""
        found: dict[str, set[str]] = {}
        for doc_id, terms in self._by_doc.items():
            for term in terms:
                found.setdefault(term, set()).add(doc_id)
        return {term: tuple(sorted(docs)) for term, docs in found.items()}

    def __len__(self) -> int:
        return sum(len(terms) for terms in self._by_doc.values())


def load_allowlist(db: Any, access_roles: Collection[str]) -> AllowList:
    """The approved entries whose `access_role` is one of `access_roles` (portable SQL; `db` is an
    app.db.connection.Database). Pending and rejected entries are never loaded."""
    roles = sorted(set(access_roles))
    if not roles:
        return AllowList()
    marks = ", ".join("?" for _ in roles)
    rows = db.query_all(
        "SELECT text, doc_id FROM allowlist_entities "
        f"WHERE status = 'approved' AND access_role IN ({marks})",
        tuple(roles),
    )
    return AllowList((text, doc_id) for text, doc_id in rows)
