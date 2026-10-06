"""Typed decisions (spec 5.1).

Jev is not a chat model. It is called through OpenRouter's Decisions API (alpha):
a state object plus typed questions in, probabilities out, no prose. Because the
endpoint is alpha, it sits behind the small DecisionClient interface, and any
surprise (bad shape, 401, 402, timeout) means "Jev unavailable" and a fallback.

The fallback deciders (Groq, Gemini, OpenRouter) get the same questions and answer
with JSON of the same shape. Their probabilities are self-reported, so those
answers are marked calibrated=False.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.gateway.errors import (
    CallTimeout,
    GatewayError,
    InvalidOutput,
    ProviderUnavailable,
    SchemaSurprise,
    status_to_error,
)

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
QUESTION_ID = re.compile(r"^[A-Za-z0-9_]+$")


# ---- questions ----------------------------------------------------------------------


@dataclass(frozen=True)
class NoulQuestion:
    """Yes/no: how likely is it that the condition holds?"""

    id: str
    instructions: str
    if_true: str
    if_false: str


@dataclass(frozen=True)
class ChoiceQuestion:
    """Pick one option. `options` maps each option to a short description."""

    id: str
    instructions: str
    options: dict[str, str]


@dataclass(frozen=True)
class ScoreQuestion:
    """Where does this fall on an ordered scale (lowest first)?"""

    id: str
    instructions: str
    scale: list[str]


Question = NoulQuestion | ChoiceQuestion | ScoreQuestion


def validate_questions(questions: list[Question]) -> None:
    seen: set[str] = set()
    if not questions:
        raise ValueError("at least one question is needed")
    for question in questions:
        if not QUESTION_ID.match(question.id) or question.id in seen:
            raise ValueError("question ids must be unique letters, digits and underscores")
        seen.add(question.id)
        if isinstance(question, ChoiceQuestion) and len(question.options) < 2:
            raise ValueError("a choice question needs at least two options")
        if isinstance(question, ScoreQuestion) and len(question.scale) < 2:
            raise ValueError("a score question needs at least two levels")


def question_payload(question: Question) -> dict[str, Any]:
    """The question in the Decisions API shape."""
    if isinstance(question, NoulQuestion):
        return {
            "type": "noul",
            "instructions": question.instructions,
            "criteria": {"true": question.if_true, "false": question.if_false},
        }
    if isinstance(question, ChoiceQuestion):
        return {
            "type": "choice",
            "instructions": question.instructions,
            "criteria": dict(question.options),
        }
    return {
        "type": "score",
        "instructions": question.instructions,
        "criteria": list(question.scale),
    }


def questions_payload(questions: list[Question]) -> dict[str, Any]:
    return {q.id: question_payload(q) for q in questions}


# ---- answers ------------------------------------------------------------------------


@dataclass(frozen=True)
class NoulAnswer:
    probability: float  # probability of yes


@dataclass(frozen=True)
class ChoiceAnswer:
    option: str
    probabilities: dict[str, float]
    confidence: float


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    probabilities: dict[str, float]
    confidence: float


Answer = NoulAnswer | ChoiceAnswer | ScoreAnswer


@dataclass
class DecisionResult:
    """One decision, with who made it. `answers` is None when the caller must use rules."""

    answers: dict[str, Answer] | None
    decider: str  # jev, groq, gemini, openrouter, rules, not_scored
    model: str | None = None
    model_snapshot: str | None = None  # the dated model the provider says it served
    provider: str | None = None
    key_fingerprint: str | None = None
    fallback_reason: str | None = None
    calibrated: bool = False  # True only for Jev; the others report their own probabilities
    cost_billed: float | None = None
    cost_source: str = "none"
    shadow_cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def needs_rules(self) -> bool:
        return self.answers is None


# ---- parsing and validation ---------------------------------------------------------


def _number(value: Any, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaSurprise()
    if not low <= float(value) <= high:
        raise SchemaSurprise()
    return float(value)


def _probabilities(raw: Any, allowed: set[str]) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise SchemaSurprise()
    result: dict[str, float] = {}
    for key, value in raw.items():
        if str(key) not in allowed:
            raise SchemaSurprise()
        result[str(key)] = _number(value, 0.0, 1.0)
    return result


def parse_answers(raw_answers: Any, questions: list[Question]) -> dict[str, Answer]:
    """Check an `answers` object against the questions. Anything unexpected is a surprise."""
    if not isinstance(raw_answers, dict):
        raise SchemaSurprise()
    parsed: dict[str, Answer] = {}
    for question in questions:
        item = raw_answers.get(question.id)
        if not isinstance(item, dict):
            raise SchemaSurprise()
        if isinstance(question, NoulQuestion):
            if item.get("type") != "noul":
                raise SchemaSurprise()
            parsed[question.id] = NoulAnswer(_number(item.get("noul"), 0.0, 1.0))
        elif isinstance(question, ChoiceQuestion):
            if item.get("type") != "choice":
                raise SchemaSurprise()
            option = item.get("choice")
            if not isinstance(option, str) or option not in question.options:
                raise SchemaSurprise()  # a list or dict here must not reach a hash lookup
            probabilities = _probabilities(item.get("probabilities"), set(question.options))
            parsed[question.id] = ChoiceAnswer(
                option, probabilities, _number(item.get("confidence"), 0.0, 1.0)
            )
        else:
            if item.get("type") != "score":
                raise SchemaSurprise()
            levels = {str(i) for i in range(len(question.scale))}
            parsed[question.id] = ScoreAnswer(
                _number(item.get("score"), 0.0, float(len(question.scale) - 1)),
                _probabilities(item.get("probabilities"), levels),
                _number(item.get("confidence"), 0.0, 1.0),
            )
    return parsed


def extract_json(text: str) -> Any:
    """Tolerant JSON parsing: strips code fences and finds the first complete object."""
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start = cleaned.find("{")
    if start < 0:
        raise InvalidOutput()
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start : index + 1])
                except (json.JSONDecodeError, RecursionError):  # bad or absurdly nested JSON
                    raise InvalidOutput() from None
    raise InvalidOutput()


def parse_fallback_answers(text: str, questions: list[Question]) -> dict[str, Answer]:
    """Validate a fallback decider's JSON. Missing confidence is derived from probabilities."""
    data = extract_json(text)
    if not isinstance(data, dict):
        raise InvalidOutput()
    try:
        prepared: dict[str, Any] = {}
        for question in questions:
            item = data.get(question.id)
            if not isinstance(item, dict):
                raise InvalidOutput()
            item = dict(item)
            item.setdefault("type", _type_name(question))
            if "confidence" not in item and isinstance(item.get("probabilities"), dict):
                values = [v for v in item["probabilities"].values() if isinstance(v, (int, float))]
                if values:
                    item["confidence"] = max(values)
            prepared[question.id] = item
        return parse_answers(prepared, questions)
    except GatewayError as error:
        if isinstance(error, InvalidOutput):
            raise
        raise InvalidOutput() from None
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
        # Whatever shape a chat model invented, it is a bad answer, not a bug of ours.
        raise InvalidOutput() from None


def _type_name(question: Question) -> str:
    if isinstance(question, NoulQuestion):
        return "noul"
    return "choice" if isinstance(question, ChoiceQuestion) else "score"


def build_fallback_messages(state: Any, questions: list[Question]) -> list[dict[str, str]]:
    """The same questions, for a chat model. The reply must be one JSON object."""
    shapes = (
        'noul: {"type":"noul","noul":<probability of yes, 0 to 1>}; '
        'choice: {"type":"choice","choice":"<option>","probabilities":{"<option>":<0 to 1>},'
        '"confidence":<0 to 1>}; '
        'score: {"type":"score","score":<number from 0 to levels-1>,'
        '"probabilities":{"0":<0 to 1>,"1":<0 to 1>},"confidence":<0 to 1>}'
    )
    system = (
        "You answer typed decision questions about the given state. Reply with one JSON "
        "object only, no prose, with one key per question id. Shapes: " + shapes
    )
    user = json.dumps(
        {"state": state, "questions": questions_payload(questions)}, ensure_ascii=False
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---- the Decisions API client ---------------------------------------------------------


class DecisionClient(Protocol):
    async def decide(
        self,
        *,
        model: str,
        state: Any,
        questions: dict[str, Any],
        api_key: str,
        timeout: float,
    ) -> dict[str, Any]:
        """Return the parsed response body. Raise a GatewayError on any failure."""
        ...


class OpenRouterDecisionsClient:
    """POST /api/alpha/decisions. Nothing from a response body is copied into errors."""

    def __init__(
        self, *, url: str = DECISIONS_URL, transport: httpx.AsyncBaseTransport | None = None
    ):
        self._url = url
        self._transport = transport

    async def decide(
        self,
        *,
        model: str,
        state: Any,
        questions: dict[str, Any],
        api_key: str,
        timeout: float,
    ) -> dict[str, Any]:
        body = {"model": model, "state": state, "questions": questions}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=timeout) as client:
                response = await client.post(self._url, json=body, headers=headers)
        except httpx.TimeoutException:
            raise CallTimeout(status=408) from None
        except httpx.HTTPError:
            raise ProviderUnavailable() from None
        if response.status_code != 200:
            raise status_to_error(response.status_code, response.headers)
        try:
            data = response.json()
        except ValueError:
            raise SchemaSurprise() from None
        if not isinstance(data, dict):
            raise SchemaSurprise()
        return data


def read_jev_response(data: dict[str, Any], questions: list[Question]) -> dict[str, Any]:
    """Pull out everything we use from a Decisions response; raise SchemaSurprise otherwise."""
    try:
        answers = parse_answers(data.get("answers"), questions)
        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            raise SchemaSurprise()
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cost = float(usage["cost"]) if usage.get("cost") is not None else None
        if input_tokens < 0 or output_tokens < 0:
            raise SchemaSurprise()
        if cost is not None and (not math.isfinite(cost) or cost < 0):
            raise SchemaSurprise()
        return {
            "answers": answers,
            "snapshot": str(data.get("model") or ""),
            "provider": str(data.get("provider") or ""),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": cost,
        }
    except GatewayError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        raise SchemaSurprise() from None
