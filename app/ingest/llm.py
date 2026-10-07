"""The model calls ingestion makes, all through the gateway (spec 5, 6.1, 6.2, 6.8).

- `caption`: one image to text (Gemini only; the gateway's `caption_image`).
- `summarise`: a table chunk's one-line summary (decide tier, no Jev: it returns no prose).
- `label`: one of the eight clause categories (decide tier, typed question).
- `classify`: how likely a candidate term is a public term of the document (decide tier).

Nothing here logs text. Text sent is the document's own text (never customer data: the command
line sends nothing for customer-specific documents).
"""

from __future__ import annotations

from typing import Any

from app.gateway import questions
from app.ingest.runner import LoopRunner

MAX_WAIT_SECONDS = 90.0
LABEL_MIN_CONFIDENCE = 0.5  # [P] below this the label is "general"
SUMMARY_MAX_CHARS = 300
STATE_MAX_CHARS = 2_500
SUMMARY_PROMPT = (
    "Write ONE plain sentence of at most 30 words saying what this table shows. "
    "No preamble, no quotes, no bullet.\n\n"
)


def sniff_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("unsupported image type")


class LlmTools:
    def __init__(self, gateway: Any, runner: LoopRunner, max_wait_s: float = MAX_WAIT_SECONDS):
        self.gateway, self.runner, self.max_wait_s = gateway, runner, max_wait_s

    def caption(self, image: bytes) -> str:
        result = self.runner.run(
            self.gateway.caption_image(image, sniff_mime(image), max_wait_s=self.max_wait_s)
        )
        return " ".join(result.text.split())

    def summarise(self, table_text: str) -> str:
        result = self.runner.run(
            self.gateway.complete(
                [{"role": "user", "content": SUMMARY_PROMPT + table_text[:STATE_MAX_CHARS]}],
                tier="decide",
                max_tokens=80,
                max_wait_s=self.max_wait_s,
            )
        )
        return " ".join(result.text.split())[:SUMMARY_MAX_CHARS]

    def label(self, text: str) -> str | None:
        """A clause category, "general" when the decider is unsure, or None when no decider answered
        at all (the caller must not save that)."""
        result = self.runner.run(
            self.gateway.decide(
                [questions.clause_label()],
                text[:STATE_MAX_CHARS],
                tier="decide",
                max_wait_s=self.max_wait_s,
            )
        )
        if result.answers is None:
            return None
        answer = result.answers.get("clause_label")
        option = getattr(answer, "option", None)
        if option in questions.CLAUSE_LABELS and getattr(answer, "confidence", 0) >= (
            LABEL_MIN_CONFIDENCE
        ):
            return option
        return "general"

    def classify(self, term: str, context: str) -> float | None:
        """The probability that `term` is a public term of the document, or None if no decider
        answered (the candidate then stays pending)."""
        state = f"Highlighted text: {term}\n\nSurrounding text: {context}"[:STATE_MAX_CHARS]
        result = self.runner.run(
            self.gateway.decide(
                [questions.entity_candidate()],
                state,
                tier="decide",
                max_wait_s=self.max_wait_s,
            )
        )
        if result.answers is None:
            return None
        answer = result.answers.get("entity_candidate")
        probability = getattr(answer, "probability", None)
        return float(probability) if probability is not None else None
