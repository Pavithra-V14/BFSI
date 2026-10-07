"""The one rule for comparing entity text (spec 6.8 "exact on normalised variants").

Ingestion stores allow-list terms and masking looks them up, so both must use this one function:
NFKC, case-folded, whitespace collapsed, and quotes, commas and brackets trimmed from the ends.
Matching is exact on this form, never fuzzy.
"""

from __future__ import annotations

import unicodedata


def normalise_entity(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(folded.split()).strip(" \t\"'`,;:()[]")
