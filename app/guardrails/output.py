"""The output guard: stage 14 (spec 4.1 step 14, 9.4, 13.2).

Checks a model's answer against the chunks it was given:
1. schema: JSON with `answer` (text) and `citations` (a list of {"chunk_id": "S1"} using the
   labels of the context block);
2. numeric check: every number, percentage, date and amount in the answer must appear in the cited
   chunks (Indian and international formats compare by value: "5 lakh" equals "5,00,000",
   "30-Sep-2026"
   equals "30 September 2026", "12.5%" equals "12.5 per cent");
3. citation check: every cited label must be one the model was given;
4. lexical groundedness: the share of the answer's content words (stemmed with the project's own
   tokenizer) found in the cited chunks must reach the threshold [T 0.6].
When the numeric or citation check fails the answer is NOT withheld: it is returned with a warning
and the unverified numbers marked. A failed groundedness check also warns and clears `grounded`.
Any warning means the answer is not cached and does not feed history (spec 8.6, 10.2).

The answer text is turned into plain text first (HTML and links stripped). Nothing here keeps or
logs the answer: the verdict carries counts, labels and the figures that failed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.guardrails import messages
from app.guardrails.context import ContextChunk
from app.guardrails.plain_text import to_plain_text
from app.vector.sparse import tokenize

GROUNDED_MIN = 0.6  # [T] share of the answer's content words found in the cited chunks
MAX_ANSWER_CHARACTERS = 6_000
MAX_CITATIONS = 10

# ---- the schema ---------------------------------------------------------------------------------
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Answer:
    text: str
    citations: tuple[str, ...]


def parse_answer(raw: str | Mapping) -> tuple[Answer | None, str | None]:
    """The answer, or (None, a short error code). Tolerates a code fence around the JSON."""
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(_FENCE.sub("", raw))
        except ValueError:
            return None, "not_json"
    if not isinstance(data, Mapping):
        return None, "not_an_object"
    text = data.get("answer")
    if not isinstance(text, str) or not text.strip():
        return None, "no_answer"
    if len(text) > MAX_ANSWER_CHARACTERS:
        return None, "answer_too_long"
    citations = data.get("citations", [])
    if not isinstance(citations, list) or len(citations) > MAX_CITATIONS:
        return None, "bad_citations"
    labels: list[str] = []
    for item in citations:
        label = item.get("chunk_id") if isinstance(item, Mapping) else item
        if not isinstance(label, str) or not label.strip():
            return None, "bad_citations"
        labels.append(label.strip())
    return Answer(text.strip(), tuple(dict.fromkeys(labels))), None


# ---- figures (the numeric check) ----------------------------------------------------------------
_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}  # fmt: skip
_MONTH = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_NUM = r"\d[\d,]*(?:\.\d+)?"
_MULTIPLIERS = {
    "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000,
    "crore": 10_000_000, "crores": 10_000_000, "cr": 10_000_000,
    "thousand": 1_000, "k": 1_000, "million": 1_000_000, "mn": 1_000_000, "billion": 10**9,
}  # fmt: skip
_UNIT = r"(lakhs?|lacs?|crores?|cr|thousand|million|mn|billion|k)\b"

_DATE_PATTERNS = (
    re.compile(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[-/. ]+{_MONTH}\.?[-/., ]+(\d{{4}}|\d{{2}})\b", re.I
    ),
    re.compile(rf"\b{_MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I),
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b"),
)
_MONTH_YEAR = re.compile(rf"\b{_MONTH}\.?\s+(\d{{4}})\b", re.I)
_PERCENT = re.compile(rf"({_NUM})\s*(?:%|per\s*cent\b|percent\b)", re.I)
_AMOUNT = re.compile(rf"(?:₹|\brs\.?|\binr\b)\s*({_NUM})(?:\s*{_UNIT})?|({_NUM})\s*{_UNIT}", re.I)
_PLAIN = re.compile(rf"(?<![\w.]){_NUM}(?![\w])")
_LIST_MARKER = re.compile(r"^[ \t]*\d{1,2}[.)](?=\s)", re.MULTILINE)


@dataclass(frozen=True)
class Figure:
    raw: str
    key: str  # a comparable form: "num:500000", "pct:12.5", "date:2026-09-30", "date:2026-09"
    start: int
    end: int


def _value(text: str, unit: str | None = None) -> str | None:
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    if unit:
        number *= _MULTIPLIERS[unit.lower()]
    return format(number.normalize(), "f")


def _iso(year: str, month: int, day: str | None = None) -> str | None:
    full = int(year) if len(year) == 4 else 2000 + int(year)
    if not 1 <= month <= 12:
        return None
    if day is None:
        return f"date:{full:04d}-{month:02d}"
    if not 1 <= int(day) <= 31:
        return None
    return f"date:{full:04d}-{month:02d}-{int(day):02d}"


def extract_figures(text: str) -> list[Figure]:
    """Every number, percentage, date and amount in `text`, by value, each once per place."""
    found: list[Figure] = []
    taken: list[tuple[int, int]] = []

    def free(start: int, end: int) -> bool:
        return all(end <= a or start >= b for a, b in taken)

    def add(match: re.Match[str], key: str | None) -> None:
        if key and free(match.start(), match.end()):
            taken.append((match.start(), match.end()))
            found.append(Figure(match.group(), key, match.start(), match.end()))

    for index, pattern in enumerate(_DATE_PATTERNS):
        for match in pattern.finditer(text):
            g = match.groups()
            if index == 0:
                key = _iso(g[2], _MONTHS[g[1].lower()], g[0])
            elif index == 1:
                key = _iso(g[2], _MONTHS[g[0].lower()], g[1])
            elif index == 2:
                key = _iso(g[0], int(g[1]), g[2])
            else:
                key = _iso(g[2], int(g[1]), g[0])
            add(match, key)
    for match in _MONTH_YEAR.finditer(text):
        add(match, _iso(match.group(2), _MONTHS[match.group(1).lower()]))
    for match in _PERCENT.finditer(text):
        value = _value(match.group(1))
        add(match, f"pct:{value}" if value else None)
    for match in _AMOUNT.finditer(text):
        number = match.group(1) or match.group(3)
        unit = match.group(2) or match.group(4)
        value = _value(number, unit)
        add(match, f"num:{value}" if value else None)
    skipped = {(m.start(), m.end()) for m in _LIST_MARKER.finditer(text)}
    for match in _PLAIN.finditer(text):
        if any(a <= match.start() < b for a, b in skipped):
            continue
        value = _value(match.group())
        add(match, f"num:{value}" if value else None)
    return sorted(found, key=lambda f: f.start)


def _evidence_keys(chunks: Iterable[ContextChunk]) -> set[str]:
    keys: set[str] = set()
    for chunk in chunks:
        pages = [str(p) for p in (chunk.page_start, chunk.page_end) if p is not None]
        text = "\n".join([chunk.text, chunk.clause_ref or "", *chunk.heading_path, *pages])
        for figure in extract_figures(text):
            keys.add(figure.key)
            if figure.key.startswith("date:") and figure.key.count("-") == 2:
                keys.add(figure.key.rsplit("-", 1)[0])  # a full date also backs "October 2026"
    return keys


def figure_is_backed(figure: Figure, evidence: set[str]) -> bool:
    if figure.key in evidence:
        return True
    if figure.key.startswith("pct:"):  # "12.5%" in the answer, a bare 12.5 in a table cell
        return "num:" + figure.key[4:] in evidence
    return False


def mark_figures(text: str, figures: Sequence[Figure]) -> str:
    """The text with each unverified figure wrapped in guillemets, for a plain-text highlight."""
    out, cursor = [], 0
    for figure in sorted(figures, key=lambda f: f.start):
        out += [text[cursor : figure.start], "«", text[figure.start : figure.end], "»"]
        cursor = figure.end
    out.append(text[cursor:])
    return "".join(out)


# ---- lexical groundedness -----------------------------------------------------------------------
_STOPWORDS = frozenset(
    "the and for are was were has have had not but with this that these those from they them their "
    "will would can could may might shall should must any all each per its our your you who whom "
    "what when where which while than then there here also only such into onto over under about "
    "after before between during within without upon been being does did doing done more most "
    "some other another one two three yes lakh lakhs lac lacs crore crores thousand million inr "
    "rupee "
    "rupees percent cent".split()
)


def content_stems(text: str) -> set[str]:
    return {
        stem
        for stem in tokenize(text)
        if len(stem) >= 3 and not stem.isdigit() and stem not in _STOPWORDS
    }


def coverage(answer_text: str, chunks: Iterable[ContextChunk]) -> float:
    wanted = content_stems(answer_text)
    if not wanted:
        return 1.0
    have: set[str] = set()
    for chunk in chunks:
        have |= content_stems(" ".join([chunk.text, *chunk.heading_path]))
    return len(wanted & have) / len(wanted)


# ---- the verdict --------------------------------------------------------------------------------
@dataclass
class OutputVerdict:
    schema_ok: bool
    schema_error: str | None = None
    text: str = ""  # the answer as plain text (empty when the schema failed)
    marked_text: str = ""  # the same with the unverified figures marked
    cited_chunk_ids: tuple[str, ...] = ()
    citation_ok: bool = False
    bad_citations: tuple[str, ...] = ()
    numeric_ok: bool = False
    unverified: tuple[Figure, ...] = ()
    grounded: bool = False
    coverage: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def has_warning(self) -> bool:
        return bool(self.warnings)


def check_output(
    raw: str | Mapping,
    *,
    chunks: Sequence[ContextChunk],
    labels: Mapping[str, str],
    curated_domains: Iterable[str] = (),
    min_coverage: float = GROUNDED_MIN,
) -> OutputVerdict:
    """`chunks` are the chunks given to the model, `labels` maps the label in the prompt ("S1") to
    a chunk id (from `build_context_block`)."""
    answer, error = parse_answer(raw)
    if answer is None:
        return OutputVerdict(False, error)
    text = to_plain_text(answer.text, curated_domains)
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    good = [label for label in answer.citations if label in labels and labels[label] in by_id]
    bad = tuple(label for label in answer.citations if label not in good)
    cited = tuple(labels[label] for label in good)
    cited_chunks = [by_id[chunk_id] for chunk_id in cited]

    verdict = OutputVerdict(True, None, text, text, cited, not bad, bad)
    evidence = _evidence_keys(cited_chunks)
    unverified = tuple(f for f in extract_figures(text) if not figure_is_backed(f, evidence))
    verdict.unverified = unverified
    verdict.numeric_ok = not unverified
    verdict.coverage = coverage(text, cited_chunks) if cited_chunks else 0.0
    verdict.grounded = bool(cited_chunks) and verdict.coverage >= min_coverage
    if unverified:
        verdict.marked_text = mark_figures(text, unverified)
        verdict.warnings.append(
            messages.WARNING_NUMBERS.format(figures=", ".join(f.raw.strip() for f in unverified))
        )
    if bad:
        verdict.warnings.append(messages.WARNING_CITATIONS)
    if not answer.citations:
        verdict.warnings.append(messages.WARNING_NO_CITATIONS)
    elif cited_chunks and not verdict.grounded:
        verdict.warnings.append(messages.WARNING_GROUNDING)
    return verdict


def has_financial_figure(text: str) -> bool:
    """True when the text carries an amount (a currency sign or word, or lakh / crore ...) or a
    percentage: the "financial-figure answer" of spec 9.5."""
    return bool(_PERCENT.search(text) or _AMOUNT.search(text))
