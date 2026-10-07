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

Known limit: `en_core_web_sm` has limited recall for Indian names (it misses some); the name
patterns and the structured patterns catch what they can. Recorded for the Phase 16 baseline.
"""

from __future__ import annotations

import re
import threading
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


# ---- structured identifiers ---------------------------------------------------------------------
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(
    r"(?<![\w.])(?:(?:\+|00)?91[ -]?)?[6-9]\d{4}[ -]?\d{5}(?!\d)"  # an Indian mobile number
    r"|(?<![\w.])\+\d{1,3}[ -]?\d{6,12}(?!\d)"  # any number written with a country code
    r"|(?<![\w.])0\d{2,4}[ -]\d{6,8}(?!\d)"  # a landline written with an STD code
)
_PAN = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]{3}[PCHFATBLJGpchfatbljg][A-Za-z]\d{4}[A-Za-z](?![A-Za-z0-9])"
)
_AADHAAR = re.compile(r"(?<!\d)[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\d)")
_CARD = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
_PASSPORT = re.compile(r"(?<![A-Za-z0-9])[A-PR-WYa-pr-wy][1-9]\d{6}(?![A-Za-z0-9])")
_KEYWORDS = (
    "policy|claim|account|a/c|acct|folio|loan|customer|cust|member|reference|ref|application|"
    "ticket|certificate|proposal|mandate|upi|pran|demat|client"
)
# "policy number is AB123456", "claim no: 98765432", "a/c # 1234567890"
_LABELLED = re.compile(
    rf"\b(?:{_KEYWORDS})\b[^\n]{{0,24}}?\b(?:no\.?|number|num|id|#)[\s:#.\-]*(?:(?:is|are)\b[\s:#.\-]*)?"
    r"([A-Za-z0-9][A-Za-z0-9/-]{4,24})",
    re.IGNORECASE,
)
# "policy AB123456" (no label word): only a code that mixes letters and digits, or a long number
_UNLABELLED = re.compile(rf"\b(?:{_KEYWORDS})\s+([A-Za-z0-9][A-Za-z0-9/-]{{5,24}})", re.IGNORECASE)
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
    for match in _CARD.finditer(text):
        digits = _digits(match.group())
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            spans.append(Span(match.start(), match.end(), "ID"))
    for match in _PASSPORT.finditer(text):
        spans.append(Span(match.start(), match.end(), "ID"))
    for match in _LABELLED.finditer(text):
        if len(_digits(match.group(1))) >= 3 and not _DOCUMENT_CODE.match(match.group(1)):
            spans.append(Span(match.start(1), match.end(1), "ID"))
    for match in _UNLABELLED.finditer(text):
        value = match.group(1)
        digits = len(_digits(value))
        mixed = digits >= 4 and any(ch.isalpha() for ch in value)
        if (mixed or digits >= 9) and not _DOCUMENT_CODE.match(value):
            spans.append(Span(match.start(1), match.end(1), "ID"))
    return spans


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

    def find(self, text: str) -> list[Span]:
        """Every span that would be masked, before the allow-list, with overlaps resolved (an
        identifier beats a phone number beats an email beats a name)."""
        candidates = _id_spans(text) + _contact_spans(text) + _name_spans(text)
        candidates += self._presidio_spans(text)
        chosen: list[Span] = []
        for span in sorted(candidates, key=lambda s: (_PRIORITY[s.kind], -(s.end - s.start))):
            if all(span.end <= other.start or span.start >= other.end for other in chosen):
                chosen.append(span)
        return sorted(chosen, key=lambda s: s.start)

    def mask(self, text: str, allow_terms: Collection[str] = ()) -> MaskResult:
        """`allow_terms` are NORMALISED terms (`normalise_entity`); a span equal to one stays."""
        allowed = allow_terms if isinstance(allow_terms, (set, frozenset)) else set(allow_terms)
        masked: list[Span] = []
        kept = 0
        for span in self.find(text):
            if normalise_entity(text[span.start : span.end]) in allowed:
                kept += 1
            else:
                masked.append(span)
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
