"""From a review-queue item to an evaluation CANDIDATE (spec 11.4, 15.2: "a thumbs-down or refused
answer can be added to the set from the review queue").

Light (no model, no heavy import): the API calls it when an admin presses "add to evaluation set".
What is stored is the MASKED question the review item already holds and the label the admin gave.
A candidate is not a case of the set: it waits (status `candidate`) until the owner cuts a new
dataset version, so that "the evaluation set and the system never change in the same week"
(spec 15.1) stays in the owner's hands. A candidate that names a document outside the sample
corpus manifest can only enter the set if the manifest is extended on purpose.
"""

from __future__ import annotations

from typing import Any

from app.eval.cases import Case

# label -> (expected behaviour, category). A "bad question" is not added.
FROM_LABEL = {
    "correct_refusal": ("refuse", None),
    "should_have_answered": ("answer", "factual"),
    "wrong_answer": ("answer", "factual"),
    "other": ("answer", "factual"),
}


def candidate_from_review(item: Any, label: str) -> Case | None:
    """The candidate case of a labelled review item, or None when it should not be added (a bad
    question, or an item with no stored question)."""
    mapped = FROM_LABEL.get(label)
    if mapped is None:
        return None
    question = str((item.details or {}).get("question") or "").strip()
    if not question:
        return None
    behaviour, category = mapped
    if category is None:
        category = "out_of_domain" if item.kind == "refused" else "factual"
    return Case(
        id=f"rq-{str(item.id).replace('-', '')[:16]}",
        category=category,
        role="customer",
        turns=(question[:500],),
        behaviour=behaviour,
        source_docs=(item.doc_id,) if item.doc_id else (),
        active=False,
        status="candidate",
        origin="review_queue",
        review_item_id=str(item.id),
        dataset_version="candidate",
    )
