"""Injection patterns and the injection decision (spec 4.1 steps 2 and 4, 9.3, 13.2).

Three pattern sets, all returning pattern NAMES only (never the matched text):
- DOCUMENT_PATTERNS: the narrow list used when a document is ingested. A policy document
  legitimately says things like "you are now entitled", so these are deliberately specific.
- query patterns: STRONG (an attack on the assistant itself: block) and WEAK (suspicious, ask the
  user to reword). A weak pattern never blocks alone, because ordinary banking questions
  ("override a standing instruction", "bypass surgery") must go through.
- chunk patterns: the document list plus a few more, used by the context guard on retrieved text.

`decide_injection` is stage 4: regex plus the Jev injection score. Any strong detector blocks;
mixed signals ask the user to reword; the detector that fired is returned so it can be logged.
The score thresholds are [T] starting values for the Phase 16 baseline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

BLOCK_SCORE = 0.85  # [T] a Jev injection score at or above this blocks
REWORD_SCORE = 0.50  # [T] from here up to BLOCK_SCORE: ask the user to reword

_FLAGS = re.IGNORECASE
Patterns = tuple[tuple[str, re.Pattern[str]], ...]

DOCUMENT_PATTERNS: Patterns = (
    (
        "ignore_instructions",
        re.compile(
            r"\b(ignore|disregard|forget)\b[^.\n]{0,25}\b(previous|prior|above|earlier|"
            r"preceding|system|all|any|your)\b[^.\n]{0,25}\b(instructions?|rules|prompts?|"
            r"guidelines|directives?)\b"
            # "override" is also what a customer does to a standing instruction: it counts only
            # when the assistant's own instructions are meant
            r"|\boverride\b[^.\n]{0,25}\b(previous|prior|above|earlier|preceding|system|your)\b"
            r"[^.\n]{0,25}\b(instructions?|rules|prompts?|guidelines|directives?)\b",
            _FLAGS,
        ),
    ),
    (
        # "show the instructions for filing a claim" is an ordinary question: asking for the
        # assistant's own prompt needs "your ...", "system prompt" or "the prompt you were given"
        "reveal_prompt",
        re.compile(
            r"\b(reveal|show|print|repeat|output|display|leak|dump)\b[^.\n]{0,20}\b(your|ur)\b"
            r"[^.\n]{0,12}\b(system prompt|prompt|instructions|rules|guidelines)\b"
            r"|\b(reveal|show|print|repeat|output|display|leak|dump)\b[^.\n]{0,20}\bsystem prompt\b"
            r"|\b(prompt|instructions|rules) (that )?you (were|have been|are) "
            r"(given|using|following)\b",
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

# Attacks on the assistant itself, in a user's question: any one of these blocks.
_QUERY_STRONG_EXTRA: Patterns = (
    ("do_anything_now", re.compile(r"\b(do anything now|developer mode|god mode)\b", _FLAGS)),
    (
        "disable_safety",
        re.compile(
            # a customer may well "remove the restrictions on my demat account": "remove" and
            # "restrictions" count only when they are the assistant's own ("your restrictions")
            r"\b(disable|turn off|bypass)\b[^.\n]{0,25}\b(safety|guardrails?|moderation|"
            r"content filters?)\b"
            r"|\b(disable|turn off|bypass|remove)\b[^.\n]{0,15}\byour\b[^.\n]{0,15}\b(safety|"
            r"filters?|guardrails?|restrictions?|moderation|rules|limits)\b",
            _FLAGS,
        ),
    ),
    (
        "no_longer_bound",
        re.compile(r"\byou are no longer\b[^.\n]{0,30}\b(assistant|bound|restricted)\b", _FLAGS),
    ),
    (
        "prompt_markup",
        re.compile(r"</?\s*(system|prompt|instructions?)\s*>|^#{2,}\s*system", _FLAGS | re.M),
    ),
    (
        "act_unrestricted",
        re.compile(r"\bact as (if )?(you (have|had) )?no (rules|restrictions|limits)\b", _FLAGS),
    ),
)
QUERY_STRONG: Patterns = (
    *(pattern for pattern in DOCUMENT_PATTERNS if pattern[0] != "system_prompt"),
    *_QUERY_STRONG_EXTRA,
)

# Suspicious on their own, not an attack: they only ask the user to reword.
QUERY_WEAK: Patterns = (
    ("system_prompt", DOCUMENT_PATTERNS[2][1]),
    ("pretend", re.compile(r"\b(pretend|role[- ]?play)\b", _FLAGS)),
    (
        "your_instructions",
        re.compile(r"\b(your|ur)\s+(instructions|rules|guidelines|prompt)\b", _FLAGS),
    ),
)

# Text retrieved from a document: the document list plus lines that address the model.
_CHUNK_EXTRA: Patterns = (
    (
        "respond_only",
        re.compile(
            r"\b(respond|reply|answer)\s+(only\s+)?with\b[^.\n]{0,30}\b(yes|no|approved)\b", _FLAGS
        ),
    ),
    (
        "dont_mention",
        re.compile(
            r"\bdo not (mention|tell|reveal|say)\b[^.\n]{0,30}\b(user|customer|anyone)\b", _FLAGS
        ),
    ),
    ("instead_of_answering", re.compile(r"\binstead of answering\b", _FLAGS)),
    ("role_line", re.compile(r"^\s*(assistant|system|user)\s*:", _FLAGS | re.M)),
    (
        "as_an_ai",
        re.compile(r"\bas an (ai|language model|assistant)\b[^.\n]{0,30}\b(you|your)\b", _FLAGS),
    ),
    ("instruction_heading", re.compile(r"^#{2,}\s*(instruction|system|prompt)", _FLAGS | re.M)),
    (
        "when_asked_say",
        re.compile(
            r"\bwhen (asked|the user asks)\b[^.\n]{0,40}\b(say|answer|respond|reply)\b", _FLAGS
        ),
    ),
)
CHUNK_PATTERNS: Patterns = (*DOCUMENT_PATTERNS, *_CHUNK_EXTRA)


def _names(patterns: Patterns, text: str) -> list[str]:
    return [name for name, pattern in patterns if pattern.search(text)]


def scan_document(text: str) -> list[str]:
    """Names of the narrow patterns that match (ingestion). Names only, never the text."""
    return _names(DOCUMENT_PATTERNS, text)


def scan_chunk(text: str) -> list[str]:
    """Names of the chunk patterns that match (the context guard)."""
    return _names(CHUNK_PATTERNS, text)


@dataclass(frozen=True)
class InjectionScan:
    strong: tuple[str, ...] = ()
    weak: tuple[str, ...] = ()


def scan_query(text: str) -> InjectionScan:
    """Stage 2.3: the regex scan of a question. It decides nothing: stage 4 does."""
    return InjectionScan(tuple(_names(QUERY_STRONG, text)), tuple(_names(QUERY_WEAK, text)))


@dataclass(frozen=True)
class InjectionDecision:
    action: str  # "pass" | "reword" | "block"
    detector: str | None = None  # "regex:<name>" or "jev", for the log
    strong: tuple[str, ...] = ()
    weak: tuple[str, ...] = ()
    jev_score: float | None = None
    reasons: list[str] = field(default_factory=list)


def decide_injection(
    scan: InjectionScan,
    jev_score: float | None,
    *,
    block_score: float = BLOCK_SCORE,
    reword_score: float = REWORD_SCORE,
) -> InjectionDecision:
    """Stage 4 [P]. A strong regex hit, or a Jev score at or above `block_score`, blocks. A weak
    regex hit, or a score from `reword_score` up to `block_score`, asks the user to reword. No Jev
    score (the decider fell back to rules) leaves the regex alone to decide."""
    common = {"strong": scan.strong, "weak": scan.weak, "jev_score": jev_score}
    if scan.strong:
        return InjectionDecision("block", f"regex:{scan.strong[0]}", **common)
    if jev_score is not None and jev_score >= block_score:
        return InjectionDecision("block", "jev", **common)
    if scan.weak:
        return InjectionDecision("reword", f"regex:{scan.weak[0]}", **common)
    if jev_score is not None and jev_score >= reword_score:
        return InjectionDecision("reword", "jev", **common)
    return InjectionDecision("pass", None, **common)
