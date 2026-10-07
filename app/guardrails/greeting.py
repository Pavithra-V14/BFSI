"""Greetings and farewells (spec 4.2, 4.1 step 2.2).

Only when the WHOLE message is one of the listed greetings or farewells does it get the fixed
reply (no model, no retrieval, no cache). "hi, what is KYC?" has a real question in it and goes
through the normal pipeline. Case, spaces and punctuation around the words do not matter.
"""

from __future__ import annotations

import re
import unicodedata

from app.guardrails import messages

GREETINGS = frozenset({"hi", "hey", "hello", "good morning"})
FAREWELLS = frozenset({"bye", "goodbye"})
THANKS = frozenset({"thanks", "thank you"})

_NOT_WORDS = re.compile(r"[^a-z\s]")


def classify_greeting(text: str) -> str | None:
    """ "greeting", "farewell", "thanks", or None when the message is anything else.

    A letter or digit other than a-z anywhere ("hello मेरा क्लेम ...", "hi 12345") means there is
    more in the message than a greeting, so it goes through the pipeline."""
    for char in text:
        if unicodedata.category(char)[0] in "LMN" and not ("a" <= char.lower() <= "z"):
            return None
    words = " ".join(_NOT_WORDS.sub(" ", text.lower()).split())
    if words in GREETINGS:
        return "greeting"
    if words in FAREWELLS:
        return "farewell"
    if words in THANKS:
        return "thanks"
    return None


def greeting_reply(kind: str) -> str:
    return {
        "greeting": messages.GREETING_REPLY,
        "farewell": messages.FAREWELL_REPLY,
        "thanks": messages.THANKS_REPLY,
    }[kind]
