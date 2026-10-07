"""The project's own sparse-vector tokenizer (spec 6.6). No fastembed.

Ingestion (documents) and the Phase 8 query side (questions) both call this module, so a word is
turned into the same number on both sides. Text is NFKC-normalised, lowercased, split into
letter and digit runs (punctuation is dropped), and lightly stemmed. Each stem becomes a stable
32-bit index (a hash, so no vocabulary file has to be kept in step); Qdrant applies IDF on its
side (the collection's sparse vector uses the IDF modifier), so only term counts are stored.

Two different stems can land on one index (a hash collision). That is rare at this vocabulary
size and only blurs two terms; it never breaks identity, which does not depend on this module.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter

from qdrant_client import models

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_MIN_STEM = 3

# Longest suffix first. Light on purpose: "claims" and "claim" meet, "premium" stays whole.
_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("ations", "ate"),
    ("ation", "ate"),
    ("ities", "ity"),
    ("ments", ""),
    ("ment", ""),
    ("ingly", ""),
    ("ies", "y"),
    ("sses", "ss"),
    ("ings", ""),
    ("ing", ""),
    ("ied", "y"),
    ("ed", ""),
    ("es", ""),
    ("ly", ""),
    ("s", ""),
)


def stem(word: str) -> str:
    """A light stem. Words with digits and short words are left alone."""
    if len(word) <= _MIN_STEM or any(char.isdigit() for char in word):
        return word
    if word.endswith("ss"):  # "class", "loss"
        return word
    for suffix, replacement in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= _MIN_STEM:
            return word[: -len(suffix)] + replacement
    return word


def tokenize(text: str) -> list[str]:
    """Lowercase stems of the letter and digit runs in `text`, in order, repeats kept."""
    folded = unicodedata.normalize("NFKC", text).lower()
    return [stem(word) for word in _WORD.findall(folded)]


def token_index(token: str) -> int:
    """A stable unsigned 32-bit index for one stem (Qdrant sparse indices are uint32)."""
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(), "big")


def _vector(counts: Counter[int], weight) -> models.SparseVector:
    indices = sorted(counts)
    return models.SparseVector(indices=indices, values=[weight(counts[i]) for i in indices])


def document_vector(text: str) -> models.SparseVector:
    """The sparse vector stored with a chunk: each term weighted 1 + ln(count)."""
    counts = Counter(token_index(token) for token in tokenize(text))
    return _vector(counts, lambda count: 1.0 + math.log(count))


def query_vector(text: str) -> models.SparseVector:
    """The sparse vector for a question: each distinct term once, weight 1."""
    counts = Counter(token_index(token) for token in tokenize(text))
    return _vector(counts, lambda count: 1.0)
