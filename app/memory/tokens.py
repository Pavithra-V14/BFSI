"""Token clipping for the history (spec 8.2). Uses the gateway's counter (`count_text`)."""

from __future__ import annotations

from typing import Any

ELLIPSIS = "…"


def clip_to_tokens(text: str, limit: int, counter: Any) -> str:
    """The text cut at a word boundary so that it, with an ellipsis, fits `limit` tokens. Text that
    already fits is returned unchanged (whitespace collapsed). Only our own history text is clipped
    this way; a user's question is never cut (the query limit refuses it instead)."""
    text = " ".join(text.split())
    if limit <= 0 or not text:
        return ""
    if counter.count_text(text) <= limit:
        return text
    words = text.split()
    low, high = 0, len(words)
    while low < high:
        middle = (low + high + 1) // 2
        if counter.count_text(" ".join(words[:middle]) + " " + ELLIPSIS) <= limit:
            low = middle
        else:
            high = middle - 1
    if low:
        return " ".join(words[:low]) + " " + ELLIPSIS
    cut = text[: max(1, limit * 3)]  # one very long word: cut characters until it fits
    while cut and counter.count_text(cut + ELLIPSIS) > limit:
        cut = cut[:-1]
    return (cut + ELLIPSIS) if cut else ""
