"""The ingestion scan for instruction-like text in a chunk (spec 9.3 "indirect injection").

A document is data, never instructions. A chunk that reads like an instruction to the model
("ignore the previous instructions", "reveal your system prompt", a chat-template marker) is held
back: it is stored but NOT active until the owner reviews it (`review chunks`). The patterns are
deliberately narrow, because a policy document legitimately says things like "you are now
entitled". The context guard (Phase 6) scans retrieved chunks again with its own, wider rules.
"""

from __future__ import annotations

import re

_FLAGS = re.IGNORECASE
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_instructions",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b[^.\n]{0,25}\b(previous|prior|above|earlier|"
            r"preceding|system|all|any|your)\b[^.\n]{0,25}\b(instructions?|rules|prompts?|"
            r"guidelines|directives?)\b",
            _FLAGS,
        ),
    ),
    (
        "reveal_prompt",
        re.compile(
            r"\b(reveal|show|print|repeat|output|display|leak)\b[^.\n]{0,20}\b(your|the)\b"
            r"[^.\n]{0,12}\b(system prompt|prompt|instructions)\b",
            _FLAGS,
        ),
    ),
    ("system_prompt", re.compile(r"\bsystem prompt\b", _FLAGS)),
    ("new_instructions", re.compile(r"\bnew instructions?\s*:", _FLAGS)),
    (
        "chat_markers",
        re.compile(r"<\|?(im_start|im_end|system|assistant)\|?>|\[/?INST\]|<<SYS>>", _FLAGS),
    ),
    (
        "role_override",
        re.compile(
            r"\b(from now on|henceforth)\b[^.\n]{0,20}\b(you|answer|respond|reply)\b", _FLAGS
        ),
    ),
    ("jailbreak", re.compile(r"\bjailbreak\b|\bDAN mode\b", _FLAGS)),
    (
        "obey_me",
        re.compile(
            r"\b(you must|you should|you will)\b[^.\n]{0,15}\b(ignore|disobey|forget|obey)\b",
            _FLAGS,
        ),
    ),
)


def scan_text(text: str) -> list[str]:
    """The names of the patterns that match (empty for clean text). Names only, never the text."""
    return [name for name, pattern in PATTERNS if pattern.search(text)]
