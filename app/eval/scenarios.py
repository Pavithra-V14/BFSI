"""Changes made to the TEST collection for the edit, delete, conflict and source-removed cases,
and the undoing of every one of them (spec 15.2).

An evaluation case may say "before turn N, withdraw this document", "edit this clause" or "a newer
version of this clause replaces the old one". The runner applies the operation to the evaluation
collection only (`check_eval_collection`), runs the turns, and undoes it, always, in a `finally`.
Because a crash could still leave a change behind, every undo step is written to a journal file
first; the next run restores from it before doing anything else.

Nothing here reads or writes the production collection: the collection name is checked on entry.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qdrant_client import models

from app.eval import constants as c
from app.eval.cases import Op
from app.vector import sparse
from app.vector.documents import DENSE_VECTOR, SPARSE_VECTOR

PAGE = 128


class ScenarioError(Exception):
    """An operation that cannot be applied (no such document or clause). Names only."""


class Scenario:
    def __init__(
        self,
        client: Any,
        collection: str,
        journal: Path,
        *,
        now: Any = lambda: datetime.now(UTC),
        embed: Any = None,
    ) -> None:
        # `embed`: an async function (list of texts) -> list of dense vectors. With it, an edited
        # or newer clause is re-embedded (one embedding request for that clause only), so the
        # dense branch of retrieval sees the new meaning too. Without it only the lexical branch
        # does.
        self._embed = embed
        self.client = client
        self.collection = c.check_eval_collection(collection)
        self.journal = journal
        self._now = now
        self._undo: list[dict[str, Any]] = self._read_journal()

    # ---- the journal ---------------------------------------------------------------------
    def _read_journal(self) -> list[dict[str, Any]]:
        if not self.journal.is_file():
            return []
        try:
            data = json.loads(self.journal.read_text("utf-8"))
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    def _write_journal(self) -> None:
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        if self._undo:
            self.journal.write_text(json.dumps(self._undo), "utf-8")
        elif self.journal.exists():
            self.journal.unlink()

    @property
    def pending(self) -> int:
        return len(self._undo)

    # ---- reading ------------------------------------------------------------------------
    def _points(self, doc_id: str, *, vectors: bool = False) -> list[Any]:
        flt = models.Filter(
            must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]
        )
        found: list[Any] = []
        offset = None
        while True:
            batch, offset = self.client.scroll(
                self.collection,
                scroll_filter=flt,
                limit=PAGE,
                offset=offset,
                with_payload=True,
                with_vectors=vectors,
            )
            found.extend(batch)
            if offset is None:
                return found

    def _matching(self, op: Op, *, vectors: bool = False) -> list[Any]:
        points = self._points(op.doc_id, vectors=vectors)
        if not points:
            raise ScenarioError("the document is not in the evaluation collection")
        if op.op == "withdraw_document":
            return points
        wanted = [p for p in points if op.anchor in str((p.payload or {}).get("text", ""))]
        if not wanted:
            raise ScenarioError("no clause of the document holds the anchor phrase")
        return wanted

    # ---- applying -----------------------------------------------------------------------
    async def apply(self, op: Op) -> None:
        if op.op == "withdraw_document":
            self._withdraw(op)
        elif op.op == "edit_text":
            await self._edit(op)
        elif op.op == "supersede_text":
            await self._supersede(op)
        else:
            raise ScenarioError("unknown operation")

    async def _dense(self, text: str, fallback: Any) -> Any:
        if self._embed is None:
            return fallback
        vectors = await self._embed([text])
        return list(vectors[0])

    def _remember(self, record: dict[str, Any]) -> None:
        self._undo.append(record)
        self._write_journal()

    def _set_effective_to(self, ids: list[Any], value: str | None) -> None:
        self.client.set_payload(
            self.collection, payload={"effective_to": value}, points=ids, wait=True
        )

    def _withdraw(self, op: Op) -> None:
        active = [
            p.id for p in self._matching(op) if (p.payload or {}).get("effective_to") in (None, [])
        ]
        if not active:
            return
        self._remember({"kind": "reactivate", "ids": [str(i) for i in active]})
        self._set_effective_to(active, self._now().isoformat())

    async def _edit(self, op: Op) -> None:
        points = self._matching(op, vectors=True)
        originals = {str(p.id): str(p.payload.get("text", "")) for p in points}
        dense_before = {str(p.id): list((p.vector or {}).get(DENSE_VECTOR) or []) for p in points}
        self._remember({"kind": "restore_text", "texts": originals, "dense": dense_before})
        for point in points:
            text = str(point.payload.get("text", "")).replace(op.old, op.new)
            dense = await self._dense(text, None)
            self._write_text(point.id, point.payload, text, dense)

    def _write_text(
        self, point_id: Any, payload: dict[str, Any], text: str, dense: Any = None
    ) -> None:
        self.client.set_payload(
            self.collection, payload={"text": text}, points=[point_id], wait=True
        )
        vectors: dict[str, Any] = {
            SPARSE_VECTOR: sparse.document_vector(
                sparse.source_text(
                    [str(h) for h in payload.get("heading_path") or []],
                    payload.get("content_type"),
                    text,
                    payload.get("summary") if isinstance(payload.get("summary"), str) else None,
                )
            )
        }
        if dense:
            vectors[DENSE_VECTOR] = list(dense)
        self.client.update_vectors(
            self.collection,
            points=[models.PointVectors(id=point_id, vector=vectors)],
            wait=True,
        )

    async def _supersede(self, op: Op) -> None:
        points = self._matching(op, vectors=True)
        copies: list[models.PointStruct] = []
        for point in points:
            payload = dict(point.payload or {})
            text = str(payload.get("text", "")).replace(op.old, op.new)
            payload.update(
                {
                    "text": text,
                    "chunk_id": f"{payload.get('chunk_id', point.id)}-v2",
                    "doc_version": int(payload.get("doc_version") or 1) + 1,
                    "effective_to": None,
                }
            )
            vectors = dict(point.vector or {})
            vectors[DENSE_VECTOR] = await self._dense(text, vectors.get(DENSE_VECTOR))
            vectors[SPARSE_VECTOR] = sparse.document_vector(
                sparse.source_text(
                    [str(h) for h in payload.get("heading_path") or []],
                    payload.get("content_type"),
                    text,
                    payload.get("summary") if isinstance(payload.get("summary"), str) else None,
                )
            )
            copies.append(models.PointStruct(id=str(uuid.uuid4()), vector=vectors, payload=payload))
        old_ids = [p.id for p in points]
        self._remember(
            {
                "kind": "unsupersede",
                "new_ids": [str(p.id) for p in copies],
                "old_ids": [str(i) for i in old_ids],
            }
        )
        self.client.upsert(self.collection, points=copies, wait=True)
        self._set_effective_to(old_ids, self._now().isoformat())

    # ---- undoing ------------------------------------------------------------------------
    def restore_all(self) -> int:
        """Undo everything, newest first. Returns how many steps were undone."""
        undone = 0
        while self._undo:
            record = self._undo[-1]
            kind = record["kind"]
            if kind == "reactivate":
                self._set_effective_to(list(record["ids"]), None)
            elif kind == "restore_text":
                dense_before = record.get("dense") or {}
                for point_id, text in record["texts"].items():
                    found = self.client.retrieve(self.collection, ids=[point_id], with_payload=True)
                    if found:
                        self._write_text(
                            point_id, found[0].payload or {}, text, dense_before.get(point_id)
                        )
            elif kind == "unsupersede":
                self.client.delete(
                    self.collection,
                    points_selector=models.PointIdsList(points=list(record["new_ids"])),
                    wait=True,
                )
                self._set_effective_to(list(record["old_ids"]), None)
            self._undo.pop()
            self._write_journal()
            undone += 1
        return undone
