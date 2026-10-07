"""PII masking of the query copy that goes to any model (spec 9.2, 4.1 step 2.4).

Runs locally, before any LLM call. Two layers:
- Presidio with the SMALL spaCy model (production uses `en_core_web_sm`) finds names, emails and
  phone numbers;
- our own patterns find structured identifiers Presidio does not know (PAN, Aadhaar, card numbers,
  passports, policy / claim / account / folio / loan numbers) and a name-introduction pattern
  ("my name is ...", "I am Rahul Sharma", "Mr. Sharma").
Everything found is replaced by [PERSON], [PHONE], [EMAIL] or [ID]. Place names are not masked [P].

The approved-entity allow-list (spec 6.8) is passed in as normalised terms; a found span whose
normalised text is in it stays as written. Two passes (decision of the owner):
1. before retrieval, with the role's approved terms, for the copy sent to models;
2. at answer time, to re-mask the copy that is stored, logged or cached, with ONLY the entries
   of the documents that survived retrieval (`AllowList.terms(doc_ids)`).
The result carries counts and offsets, never the matched text.

Before anything is looked for, the text is viewed in a normal form (NFKC, zero-width and bidi
characters removed, every Unicode space and digit turned into the plain one) so that a number
written with a non-breaking space, full-width digits or Devanagari digits is found; each span
found is mapped back to the ORIGINAL text, which is what is replaced. Names typed in ALL CAPS or
all lowercase get a second look at a copy with the name-like words capitalised (`_title_runs`).

Known limit: `en_core_web_sm` has limited recall for Indian names (it misses some); the name
patterns and the structured patterns catch what they can. Recorded for the Phase 16 baseline.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from app.guardrails.normalise import normalise_entity

PLACEHOLDERS = {
    "PERSON": "[PERSON]",
    "PHONE": "[PHONE]",
    "EMAIL": "[EMAIL]",
    "ID": "[ID]",
}
_PRIORITY = {"ID": 0, "PHONE": 1, "EMAIL": 2, "PERSON": 3}
MODEL_NAME = "en_core_web_sm"
PRESIDIO_ENTITIES = ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER"]
MIN_PRESIDIO_SCORE = 0.4
CASED_NAME_SCORE = 0.6  # [T] the second look at capitals / lowercase names asks for more

# Words that mark a "name" the small model found as a banking term, not a person.
DOMAIN_WORDS = frozenset(
    "policy wording premium claim claims insured insurance insurer loan interest tenor tenure fund "
    "scheme plan account statement cover coverage benefit exclusion clause section schedule "
    "liability cap grievance officer bank bharosa riskometer kyc nav emi sip ombudsman irdai sebi "
    "rbi turnaround deductible nominee annuity bonus surrender maturity rider gstin ifsc neft rtgs "
    "imps upi gst tds itr ppf nps epf ulip aadhaar".split()
)
# Product tier names: "Silver plan", "Gold card" are products, not people.
PRODUCT_TIERS = frozenset(
    "gold silver platinum bronze diamond titanium classic premier prime royal smart super elite "
    "basic standard flexi max star lite plus pro".split()
)
_PRODUCT_NOUNS = frozenset(
    "plan policy scheme fund account card loan cover insurance product variant series".split()
)
_STOP_AFTER_NAME = frozenset(
    "and i my is am from with in at for of the a an to how what can will would should could "
    "when where which who why do does did please about regarding on by via using tomorrow today "
    "now later".split()
)
_NOT_A_NAME = frozenset(
    "looking interested not also trying wondering asking writing calling new here sure unable "
    "having planning thinking applying a an the".split()
)


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    kind: str  # PERSON | PHONE | EMAIL | ID


@dataclass(frozen=True)
class MaskResult:
    text: str
    spans: tuple[Span, ...] = ()  # offsets in the ORIGINAL text; never the matched text
    kept: int = 0  # spans left as written because they are on the allow-list
    counts: dict[str, int] = field(default_factory=dict)


# ---- the normalised view ------------------------------------------------------------------------
_INVISIBLE = frozenset(
    chr(code)
    for code in (
        0x00AD,  # soft hyphen
        0x034F,  # combining grapheme joiner
        0x061C,  # Arabic letter mark
        0x180E,  # Mongolian vowel separator
        0xFEFF,  # byte-order mark / zero-width no-break space
        *range(0x200B, 0x2010),  # zero-width space .. right-to-left mark
        *range(0x202A, 0x202F),  # bidi embeddings and overrides
        *range(0x2060, 0x2070),  # word joiner, invisible operators, isolates
    )
)


class View:
    """The text in the form the patterns expect, with each character's place in the original.

    NFKC turns full-width letters and digits into plain ones and every Unicode space into a plain
    space; Devanagari, Arabic-Indic and other decimal digits become 0-9; zero-width and bidi
    characters are dropped. Plain ASCII text is its own view."""

    def __init__(self, original: str) -> None:
        self.original = original
        self._starts: list[int] | None = None
        self._ends: list[int] | None = None
        if original.isascii():
            self.text = original
            return
        out: list[str] = []
        starts: list[int] = []
        ends: list[int] = []
        for index, char in enumerate(original):
            if char in _INVISIBLE:
                continue
            for piece in unicodedata.normalize("NFKC", char):
                if piece.isspace() and not piece.isascii():
                    piece = " "
                elif piece.isdecimal() and not piece.isascii():
                    piece = str(unicodedata.decimal(piece))
                out.append(piece)
                starts.append(index)
                ends.append(index + 1)
        self.text = "".join(out)
        self._starts, self._ends = starts, ends

    def to_original(self, span: Span) -> Span:
        if self._starts is None or self._ends is None:
            return span
        return Span(self._starts[span.start], self._ends[span.end - 1], span.kind)


# ---- structured identifiers ---------------------------------------------------------------------
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_SEP = r"[ .\-/]"  # what people put between groups of digits
_PHONE = re.compile(
    rf"(?<![\w.])(?:(?:\+|00)?91{_SEP}?)?[6-9]\d{{4}}{_SEP}?\d{{5}}(?!\d)"  # an Indian mobile
    rf"|(?<![\w.])\+\d{{1,3}}{_SEP}?\d{{6,12}}(?!\d)"  # any number written with a country code
    rf"|(?<![\w.])0\d{{2,4}}{_SEP}\d{{6,8}}(?!\d)"  # a landline written with an STD code
)
_PAN = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]{3}[PCHFATBLJGpchfatbljg][A-Za-z]\d{4}[A-Za-z](?![A-Za-z0-9])"
)
_AADHAAR = re.compile(rf"(?<!\d)[2-9]\d{{3}}{_SEP}?\d{{4}}{_SEP}?\d{{4}}(?!\d)")
# a run of digit groups; the card number is searched for inside it, group by group
_DIGIT_RUN = re.compile(rf"(?<!\d)\d+(?:{_SEP}\d+){{0,12}}(?!\d)")
_PASSPORT = re.compile(r"(?<![A-Za-z0-9])[A-PR-WYa-pr-wy][1-9]\d{6}(?![A-Za-z0-9])")
_KEYWORDS = (
    "policy|claim|account|a/c|acct|folio|loan|customer|cust|member|reference|ref|application|"
    "ticket|certificate|proposal|mandate|upi|pran|demat|client"
)
# A value may be written in groups: "1234 5678 9012", "12 345 678" (a group is 2 or more digits).
_VALUE = r"[A-Za-z0-9][A-Za-z0-9/-]{0,24}(?: \d{2,}[A-Za-z0-9/-]*){0,5}"
# "policy number is AB123456", "claim no: 98765432", "a/c # 1234567890"
_LABELLED = re.compile(
    rf"\b(?:{_KEYWORDS})\b[^\n]{{0,24}}?\b(?:no\.?|number|num|id|#)[\s:#.\-]*"
    rf"(?:(?:is|are)\b[\s:#.\-]*)?({_VALUE})",
    re.IGNORECASE,
)
# "policy AB123456" (no label word): only a code that mixes letters and digits, or a long number
_UNLABELLED = re.compile(rf"\b(?:{_KEYWORDS})\s+({_VALUE})", re.IGNORECASE)
_STRONG_INTRO = re.compile(
    r"\b(?:my name is|my name's|name\s*[:\-]|call me|myself)\s+"
    r"([A-Za-z][A-Za-z'.-]*(?:\s+[A-Za-z][A-Za-z'.-]*){0,2})",
    re.IGNORECASE,
)
_SOFT_INTRO = re.compile(
    r"\b(?:[Ii] am|[Ii]'m|[Tt]his is)\s+([A-Z][a-z'.-]+(?:\s+[A-Z][a-z'.-]+){0,2})"
)
_TITLED = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Shri|Smt|Prof)\.?\s+([A-Z][a-z'-]+(?:\s+[A-Z][a-z'-]+){0,2})"
)

_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5), (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7), (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3), (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)  # fmt: skip
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4), (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7), (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)  # fmt: skip


def verhoeff_ok(number: str) -> bool:
    """The checksum of an Aadhaar number (it rejects most random 12-digit strings)."""
    check = 0
    for position, digit in enumerate(reversed(number)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[position % 8][int(digit)]]
    return check == 0


def luhn_ok(number: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(number)):
        digit = int(char)
        if position % 2 == 1:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


_TOLL_FREE = re.compile(r"1(?:800|860)")  # a public helpline number is not a private phone
_DOCUMENT_CODE = re.compile(r"(?:[A-Za-z]{2,}[-/]){2,}")  # AV-GI-FCP-2026-0518: a document code


def _is_a_product_not_a_person(text: str, start: int, end: int) -> bool:
    """The small model tags "Silver", "Max Tenor" or "GSTIN" as people. A name made only of banking
    terms or product tiers, or one word in front of a product noun ("Silver plan"), is not one."""
    words = [w.strip(".'-").lower() for w in re.findall(r"[A-Za-z][A-Za-z'.-]*", text[start:end])]
    if not words:
        return True
    if any(w in DOMAIN_WORDS for w in words) or all(w in PRODUCT_TIERS for w in words):
        return True
    following = re.match(r"\s+([A-Za-z]+)", text[end:])
    return len(words) == 1 and bool(following) and following.group(1).lower() in _PRODUCT_NOUNS


def _id_spans(text: str) -> list[Span]:
    spans: list[Span] = []
    for match in _PAN.finditer(text):
        spans.append(Span(match.start(), match.end(), "ID"))
    for match in _AADHAAR.finditer(text):
        digits = _digits(match.group())
        if len(digits) == 12 and verhoeff_ok(digits):
            spans.append(Span(match.start(), match.end(), "ID"))
    for match in _DIGIT_RUN.finditer(text):
        spans.extend(_card_spans(match))
    for match in _PASSPORT.finditer(text):
        spans.append(Span(match.start(), match.end(), "ID"))
    for match in _LABELLED.finditer(text):
        value = match.group(1)
        if _labelled_value_ok(value) and not _DOCUMENT_CODE.match(value):
            spans.append(Span(match.start(1), match.end(1), "ID"))
    for match in _UNLABELLED.finditer(text):
        value = match.group(1)
        digits = len(_digits(value))
        tokens = value.split()
        long_enough = len(tokens[0]) >= (6 if len(tokens) == 1 else 4)
        mixed = digits >= 4 and any(ch.isalpha() for ch in value)
        if long_enough and (mixed or digits >= 9) and not _DOCUMENT_CODE.match(value):
            spans.append(Span(match.start(1), match.end(1), "ID"))
    return spans


def _labelled_value_ok(value: str) -> bool:
    """A value after "policy number" ...: at least 5 characters (spaces apart) and 3 digits."""
    return len(value.replace(" ", "")) >= 5 and len(_digits(value)) >= 3


def _card_spans(run: re.Match[str]) -> list[Span]:
    """The card number inside a run of digit groups. Every window of whole groups holding 13 to 19
    digits is tried, so a stray "12" in front ("12 4111 1111 1111 1111") cannot spoil the check.
    Windows that pass the Luhn check and overlap are merged: when a chance window (a date and part
    of the card) also passes, the whole of the card is still covered, never part of it."""
    text = run.group()
    groups = [(m.start(), m.end()) for m in re.finditer(r"\d+", text)]
    windows: list[tuple[int, int]] = []
    for first in range(len(groups)):
        for last in range(first, len(groups)):
            start, end = groups[first][0], groups[last][1]
            digits = _digits(text[start:end])
            if len(digits) > 19:
                break
            if len(digits) >= 13 and luhn_ok(digits):
                windows.append((start, end))
    merged: list[list[int]] = []
    for start, end in sorted(windows):
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [Span(run.start() + a, run.start() + b, "ID") for a, b in merged]


def _name_spans(text: str) -> list[Span]:
    spans: list[Span] = []

    def take(match: re.Match[str], capitalised_only: bool) -> None:
        words = list(re.finditer(r"[A-Za-z][A-Za-z'.-]*", match.group(1)))
        taken = []
        for word in words:
            low = word.group().strip(".'-").lower()
            if low in _STOP_AFTER_NAME or low in _NOT_A_NAME or low in DOMAIN_WORDS:
                break
            if capitalised_only and not word.group()[0].isupper():
                break
            taken.append(word)
        if taken:
            start = match.start(1) + taken[0].start()
            last = taken[-1]
            end = match.start(1) + last.start() + len(last.group().rstrip(".'-"))
            if end > start:
                spans.append(Span(start, end, "PERSON"))

    for match in _STRONG_INTRO.finditer(text):
        take(match, capitalised_only=False)
    for pattern in (_SOFT_INTRO, _TITLED):
        for match in pattern.finditer(text):
            take(match, capitalised_only=True)
    return spans


def _contact_spans(text: str) -> list[Span]:
    spans = [Span(m.start(), m.end(), "EMAIL") for m in _EMAIL.finditer(text)]
    spans += [Span(m.start(), m.end(), "PHONE") for m in _PHONE.finditer(text)]
    return spans


# ---- names typed in capitals or in lowercase ----------------------------------------------------
# The name model is trained on text where names start with a capital letter. For "RAHUL SHARMA" or
# "priya nair" the words that could be a name get a second look in a copy where they are
# capitalised (same length, so the spans map straight back). Everything below only ever ADDS
# spans; it never unmasks anything, and the allow-list and the product-word protections still
# apply to what it finds.
COMMON_WORDS = frozenset(
    "the a an and or but if of to in on at for from with without by as is are was were be been "
    "being am i my me we our you your he his she her it its they their this that these those what "
    "which who whom how when where why can could will would shall should may might must do does "
    "did has have had not no yes please also about regarding over under after before between into "
    "than then so too very just only any all some each every more most other such own want wants "
    "wanted need needs needed help helps helped ask asks asked asking know tell told say said says "
    "call called calls calling lost lose loses get gets got give gives gave make makes made take "
    "takes took pay pays paid paying check checks checked update updates updated change changes "
    "changed apply applied applies report reported reports wrote write writes send sends sent open "
    "opened close closed cancel cancelled renew renewed wish wishes like likes here there today "
    "tomorrow yesterday now later again still already pending rejected approved denied refused "
    "settled delayed issue problem complaint request requests status details detail info "
    "information query question document documents form forms number copy new old explain show "
    "list download upload contact email phone mobile address name names".split()
)
_NOT_NAME_WORDS = COMMON_WORDS | DOMAIN_WORDS | PRODUCT_TIERS | _PRODUCT_NOUNS
_ASCII_WORD = re.compile(r"[A-Za-z]+")
_RUN_GAP = re.compile(r" {1,2}")
MAX_NAME_WORDS = 4


def _title_runs(text: str) -> tuple[str, list[tuple[int, int]]]:
    """A copy of `text` (same length) where each run of 2 to 4 words that could be a name has its
    words capitalised, and the places of those runs. A word could be a name when it is not an
    ordinary word of a banking question; a run needs at least one word typed wholly in capitals or
    wholly in lowercase (a name already written as "Rahul Sharma" is the first pass's business)."""
    words = list(_ASCII_WORD.finditer(text))
    chars = list(text)
    runs: list[tuple[int, int]] = []
    for word in words:
        piece = word.group()
        if len(piece) >= 2 and piece.isupper() and piece.lower() in _NOT_NAME_WORDS:
            chars[word.start() : word.end()] = piece.lower()  # "HAS A POLICY": calm the capitals
    candidates = [
        w for w in words if len(w.group()) >= 2 and w.group().lower() not in _NOT_NAME_WORDS
    ]
    index = 0
    while index < len(candidates):
        end = index
        while end + 1 < len(candidates) and _RUN_GAP.fullmatch(
            text[candidates[end].end() : candidates[end + 1].start()]
        ):
            end += 1
        run = candidates[index : end + 1]
        index = end + 1
        uniform = [w for w in run if w.group().islower() or w.group().isupper()]
        if 2 <= len(run) <= MAX_NAME_WORDS and uniform:
            for word in run:
                piece = word.group()
                if piece.islower() or piece.isupper():
                    chars[word.start() : word.end()] = piece[0].upper() + piece[1:].lower()
            runs.append((run[0].start(), run[-1].end()))
    return "".join(chars), runs


# ---- Presidio -----------------------------------------------------------------------------------
def build_analyzer(model: str = MODEL_NAME) -> Any:
    """A Presidio analyzer on the small spaCy model, with the parts it does not need switched off
    (the parser, tagger and lemmatizer): the name model still runs, about three times faster."""
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": model}],
        }
    )
    engine = AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=["en"])
    nlp = engine.nlp_engine.nlp["en"]
    for pipe in ("parser", "tagger", "attribute_ruler", "lemmatizer"):
        if pipe in nlp.pipe_names:
            nlp.disable_pipe(pipe)
    return engine


class Masker:
    """Masks text. `analyzer` is a Presidio analyzer (built on first use when not given)."""

    def __init__(self, analyzer: Any = None) -> None:
        self._analyzer = analyzer
        self._lock = threading.Lock()

    def _engine(self) -> Any:
        if self._analyzer is None:
            with self._lock:
                if self._analyzer is None:
                    self._analyzer = build_analyzer()
        return self._analyzer

    def _presidio_spans(self, text: str) -> list[Span]:
        spans: list[Span] = []
        results = self._engine().analyze(
            text=text, language="en", entities=PRESIDIO_ENTITIES, score_threshold=MIN_PRESIDIO_SCORE
        )
        for hit in results:
            found = text[hit.start : hit.end]
            if hit.entity_type == "PERSON":
                if _is_a_product_not_a_person(text, hit.start, hit.end):
                    continue
                spans.append(Span(hit.start, hit.end, "PERSON"))
            elif hit.entity_type == "EMAIL_ADDRESS":
                spans.append(Span(hit.start, hit.end, "EMAIL"))
            elif (
                hit.entity_type == "PHONE_NUMBER"
                and len(_digits(found)) >= 10
                and not _TOLL_FREE.match(_digits(found))
            ):
                spans.append(Span(hit.start, hit.end, "PHONE"))
        return spans

    def _cased_name_spans(self, text: str) -> list[Span]:
        """Names typed in capitals or lowercase: the name model looks again at a copy where the
        words that could be a name are capitalised. A name needs two words or more here (a single
        lowercase word is far too often an ordinary word); the words must lie inside one capitalised
        run, and the product-word protection applies as before."""
        titled, runs = _title_runs(text)
        if not runs:
            return []
        spans: list[Span] = []
        for match in _TITLED.finditer(titled):  # "MR SHARMA", "dr anita rao"
            words = list(_ASCII_WORD.finditer(match.group(1)))
            if words and not any(w.group().lower() in _NOT_NAME_WORDS for w in words):
                spans.append(Span(match.start(1), match.start(1) + words[-1].end(), "PERSON"))
        hits = self._engine().analyze(
            text=titled, language="en", entities=["PERSON"], score_threshold=CASED_NAME_SCORE
        )
        for hit in hits:
            for first, last in runs:
                start, end = max(hit.start, first), min(hit.end, last)
                if end <= start:
                    continue
                # only the words really typed in capitals / lowercase: a name already written
                # "Anita Rao" was the first pass's, and must not drag a lowercase "writing" along
                best: list[re.Match[str]] = []
                current: list[re.Match[str]] = []
                for word in _ASCII_WORD.finditer(text, start, end):
                    uniform = word.group().islower() or word.group().isupper()
                    current = current + [word] if uniform else []
                    best = max(best, current, key=len)
                if len(best) >= 2 and not _is_a_product_not_a_person(
                    text, best[0].start(), best[-1].end()
                ):
                    spans.append(Span(best[0].start(), best[-1].end(), "PERSON"))
        return spans

    def _detect(self, text: str) -> list[Span]:
        """Every span that would be masked, in the offsets of `text`, with overlaps resolved (an
        identifier beats a phone number beats an email beats a name)."""
        candidates = _id_spans(text) + _contact_spans(text) + _name_spans(text)
        candidates += self._presidio_spans(text)
        candidates += self._cased_name_spans(text)
        chosen: list[Span] = []
        for span in sorted(candidates, key=lambda s: (_PRIORITY[s.kind], -(s.end - s.start))):
            if all(span.end <= other.start or span.start >= other.end for other in chosen):
                chosen.append(span)
        return sorted(chosen, key=lambda s: s.start)

    def find(self, text: str) -> list[Span]:
        """Every span that would be masked, before the allow-list, in the offsets of `text`."""
        view = View(text)
        return [view.to_original(span) for span in self._detect(view.text)]

    def mask(self, text: str, allow_terms: Collection[str] = ()) -> MaskResult:
        """`allow_terms` are NORMALISED terms (`normalise_entity`); a span equal to one stays."""
        allowed = allow_terms if isinstance(allow_terms, (set, frozenset)) else set(allow_terms)
        view = View(text)
        masked: list[Span] = []
        kept = 0
        for found in self._detect(view.text):
            if normalise_entity(view.text[found.start : found.end]) in allowed:
                kept += 1
            else:
                masked.append(view.to_original(found))
        pieces: list[str] = []
        cursor = 0
        for span in masked:
            pieces.append(text[cursor : span.start])
            pieces.append(PLACEHOLDERS[span.kind])
            cursor = span.end
        pieces.append(text[cursor:])
        counts: dict[str, int] = {}
        for span in masked:
            counts[span.kind] = counts.get(span.kind, 0) + 1
        return MaskResult("".join(pieces), tuple(masked), kept, counts)


_shared: Masker | None = None
_shared_lock = threading.Lock()


def shared_masker() -> Masker:
    """One process-wide masker, so the model is loaded once."""
    global _shared
    if _shared is None:
        with _shared_lock:
            if _shared is None:
                _shared = Masker()
    return _shared
