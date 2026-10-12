"""Shared by `scripts/record_cassettes.py` (owner-run, real calls) and the replay tests.

A cassette is a recording of one real provider answer (VCR.py, JSON). What makes it safe to
commit is done here, in code, before anything reaches a file:

- every request header that can carry a key is replaced (Authorization, x-goog-api-key, ...)
  and every key-like query parameter (`key=`, `api_key=`) is replaced;
- every response header is dropped except the content type;
- embedding vectors are cut to their first `KEEP_VALUES` numbers, with the original length kept
  next to them so a replay can put the vector back to its real length (zeros after the kept
  numbers): the shape and the length survive, the thousands of numbers do not;
- any long, high-entropy token in a response body (a request id, a generation id) is replaced;
- `hygiene_problems` is run over the finished file and the recorder deletes a file that fails.

The replay never reaches a network: `record_mode="none"`, and a request that is not in the
cassette is an error, not a call.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import vcr

CASSETTE_DIR = Path(__file__).resolve().parents[1] / "tests" / "cassettes"
REDACTED = "REDACTED"
KEEP_VALUES = 8  # how many numbers of an embedding vector are kept
LENGTH_SUFFIX = "_original_length"

SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-goog-api-key",
    "x-api-key",
    "api-key",
    "cookie",
    "set-cookie",
}
SENSITIVE_PARAMETERS = {"key", "api_key", "apikey", "token", "access_token", "auth"}
KEPT_RESPONSE_HEADERS = {"content-type"}

# the five providers of the spec (section 16) and the file each one is recorded in
NAMES = ("cohere_rerank", "jina_rerank", "jev_decisions", "gemini_embedding", "openrouter_chat")

_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-+=]{32,}")  # no "/": a URL path is not a token
_KEY_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"(?i)bearer\s+(?!REDACTED)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)[?&](?:key|api_key|apikey|token|access_token)=(?!REDACTED)[^&\"\\\s]{4,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
ENTROPY_LIMIT = 3.5  # bits per character; a long identifier or a key is above it


def entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


# ----------------------------------------------------------------------------------- scrubbing
def _scrub_uri(uri: str) -> str:
    parts = urlsplit(uri)
    pairs = [
        (k, REDACTED if k.lower() in SENSITIVE_PARAMETERS else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(pairs)))


# Request body fields that describe the CALL and carry no user text: kept as they are. Every
# other string in a request body (a prompt, a question, a passage) is replaced.
SAFE_BODY_FIELDS = {
    "model", "dimensions", "top_n", "max_tokens", "temperature", "task_type", "type",
    "response_format", "reasoning_effort", "encoding_format", "role", "output_dimensionality",
}  # fmt: skip
BODY_PLACEHOLDER = "REDACTED-TEXT"


def _scrub_body_value(value: Any, field: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _scrub_body_value(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_body_value(v, field) for v in value]
    if isinstance(value, str) and field not in SAFE_BODY_FIELDS:
        return BODY_PLACEHOLDER
    return value


def _scrub_body(body: Any) -> Any:
    """Keep the shape of a JSON request body (so the cassette still documents the call) and
    replace every text in it that is not a call parameter. A body that is not JSON is dropped."""
    if body in (None, b"", ""):
        return body
    raw = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    try:
        data = json.loads(raw)
    except ValueError:
        return b"REDACTED-BODY" if isinstance(body, bytes) else "REDACTED-BODY"
    cleaned = json.dumps(_scrub_body_value(data))
    return cleaned.encode("utf-8") if isinstance(body, bytes) else cleaned


def scrub_request(request: Any) -> Any:
    """`before_record_request`: header values (a list value too, as VCR keeps them), key-like
    query parameters and the text of the body are replaced."""
    for name in list(request.headers):
        if name.lower() in SENSITIVE_HEADERS:
            value = request.headers[name]
            request.headers[name] = [REDACTED] * len(value) if isinstance(value, list) else REDACTED
    request.uri = _scrub_uri(request.uri)
    request.body = _scrub_body(request.body)
    return request


def _scrub_strings(value: Any) -> Any:
    if isinstance(value, str):
        return _LONG_TOKEN.sub(
            lambda m: REDACTED + "-TOKEN" if entropy(m.group(0)) >= ENTROPY_LIMIT else m.group(0),
            value,
        )
    if isinstance(value, list):
        return [_scrub_strings(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_strings(v) for k, v in value.items()}
    return value


def _shorten_vectors(value: Any) -> Any:
    """Cut every list of more than KEEP_VALUES numbers; keep its length beside it."""
    if isinstance(value, list):
        return [_shorten_vectors(v) for v in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            numbers = (
                isinstance(item, list)
                and len(item) > KEEP_VALUES
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in item)
            )
            if numbers:
                out[key] = item[:KEEP_VALUES]
                out[key + LENGTH_SUFFIX] = len(item)
            else:
                out[key] = _shorten_vectors(item)
        return out
    return value


def _lengthen_vectors(value: Any) -> Any:
    """The replay side: put every cut vector back to its recorded length with zeros."""
    if isinstance(value, list):
        return [_lengthen_vectors(v) for v in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key.endswith(LENGTH_SUFFIX):
                continue
            wanted = value.get(key + LENGTH_SUFFIX)
            if isinstance(item, list) and isinstance(wanted, int) and wanted > len(item):
                out[key] = list(item) + [0.0] * (wanted - len(item))
            else:
                out[key] = _lengthen_vectors(item)
        return out
    return value


def scrub_response(response: dict[str, Any]) -> dict[str, Any]:
    """`before_record_response`: only the content type is kept of the headers; a JSON body has
    its vectors cut and its long tokens replaced."""
    response["headers"] = {
        k: v for k, v in response.get("headers", {}).items() if k.lower() in KEPT_RESPONSE_HEADERS
    }
    body = response.get("body", {})
    text = body.get("string")
    was_bytes = isinstance(text, bytes)  # VCR also filters on replay, where it wants bytes back
    if was_bytes:
        text = text.decode("utf-8", errors="replace")
    if isinstance(text, str):
        try:
            data = json.loads(text)
        except ValueError:
            cleaned = _scrub_strings(text)
        else:
            cleaned = json.dumps(_scrub_strings(_shorten_vectors(data)))
        body["string"] = cleaned.encode("utf-8") if was_bytes else cleaned
    return response


class PaddedJson:
    """The cassette serializer: plain JSON on disk; vectors are put back to length on load."""

    @staticmethod
    def serialize(cassette: dict[str, Any]) -> str:
        return json.dumps(cassette, indent=2, sort_keys=True) + "\n"

    @staticmethod
    def deserialize(text: str) -> dict[str, Any]:
        data = json.loads(text)
        for interaction in data.get("interactions", []):
            body = interaction.get("response", {}).get("body", {})
            raw = body.get("string")
            if isinstance(raw, str):
                try:
                    body["string"] = json.dumps(_lengthen_vectors(json.loads(raw)))
                except ValueError:
                    pass
        return data


def cassette_path(name: str) -> Path:
    return CASSETTE_DIR / f"{name}.json"


def make_vcr(record_mode: str = "none") -> vcr.VCR:
    """`none`: replay only, a request that is not recorded is an error (the tests). `all`: call
    the real provider and write the cassette (the recorder).

    The scrubbing hooks belong to recording only: VCR would otherwise run them again on every
    replayed response and cut the restored vectors back down."""
    recording = record_mode != "none"
    recorder = vcr.VCR(
        record_mode=record_mode,
        match_on=("method", "scheme", "host", "path"),  # not the query: it held a key
        before_record_request=scrub_request if recording else None,
        before_record_response=scrub_response if recording else None,
        decode_compressed_response=True,
    )
    recorder.register_serializer("padded_json", PaddedJson)
    recorder.serializer = "padded_json"
    return recorder


def use_cassette(name: str, record_mode: str = "none"):
    return make_vcr(record_mode).use_cassette(str(cassette_path(name)), allow_playback_repeats=True)


# -------------------------------------------------------------------------------- the hygiene
def _header_problems(node: Any) -> bool:
    """True when a credential header anywhere in the parsed cassette holds anything but the
    placeholder (a string, or a list of strings as VCR stores them)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() in SENSITIVE_HEADERS:
                values = value if isinstance(value, list) else [value]
                if any(v != REDACTED for v in values):
                    return True
            if _header_problems(value):
                return True
    elif isinstance(node, list):
        return any(_header_problems(item) for item in node)
    return False


def hygiene_problems(text: str, forbidden: Sequence[str] = ()) -> list[str]:
    """Names of the rules a cassette's text breaks (never the offending text itself).

    `forbidden` are the real key values the recorder used: none of them may appear anywhere."""
    found: list[str] = []
    for index, shape in enumerate(_KEY_SHAPES):
        if shape.search(text):
            found.append(f"key-shaped value (rule {index + 1})")
    for token in _LONG_TOKEN.findall(text):
        if entropy(token) >= ENTROPY_LIMIT and token != REDACTED + "-TOKEN":
            found.append("long high-entropy string")
            break
    try:
        structured = json.loads(text)
    except ValueError:
        structured = None
    if structured is not None:
        if _header_problems(structured):
            found.append("a credential header that is not replaced")
    else:
        for match in re.finditer(
            r'"(?i:authorization|x-goog-api-key|x-api-key|api-key)"\s*:\s*', text
        ):
            tail = text[match.end() : match.end() + 60].lstrip("[ ")
            if not tail.startswith('"' + REDACTED):
                found.append("a credential header that is not replaced")
                break
    if any(value and len(value) >= 8 and value in text for value in forbidden):
        found.append("a real key value is in the file")
    return found


# ------------------------------------------------------------------------------- the scenarios
RERANK_QUERY = "What is the waiting period for illness claims?"
RERANK_DOCS = [
    "A waiting period of 30 days applies to illness claims from the policy start date.",
    "Premiums are payable annually on or before the due date shown in the schedule.",
    "Accident claims have no waiting period and are covered from the first day.",
]
EMBED_INPUTS = [
    "title: none | text: A waiting period of 30 days applies to illness claims.",
    "title: none | text: Premiums are payable annually.",
]
CHAT_MESSAGES = [
    {"role": "system", "content": "Answer in one short sentence."},
    {"role": "user", "content": "What is a waiting period in an insurance policy?"},
]

Scenario = Callable[[dict[str, str]], Any]


async def cohere_rerank(keys: dict[str, str]) -> Any:
    from app.gateway.config import DEFAULT_MODELS
    from app.gateway.rerank import http_rerank

    return await http_rerank(
        "cohere",
        model=DEFAULT_MODELS["COHERE_RERANK_MODEL"],
        query=RERANK_QUERY,
        documents=RERANK_DOCS,
        top_n=2,
        api_key=keys["cohere"],
        timeout=30.0,
    )


async def jina_rerank(keys: dict[str, str]) -> Any:
    from app.gateway.config import DEFAULT_MODELS
    from app.gateway.rerank import http_rerank

    return await http_rerank(
        "jina",
        model=DEFAULT_MODELS["JINA_RERANK_MODEL"],
        query=RERANK_QUERY,
        documents=RERANK_DOCS,
        top_n=2,
        api_key=keys["jina"],
        timeout=30.0,
    )


def jev_questions() -> list[Any]:
    from app.gateway import questions as qs

    return [qs.domain_relevance(), qs.vague_first_message()]


async def jev_decisions(keys: dict[str, str]) -> Any:
    from app.gateway.config import DEFAULT_MODELS
    from app.gateway.decisions import OpenRouterDecisionsClient, questions_payload

    return await OpenRouterDecisionsClient().decide(
        model=DEFAULT_MODELS["JEV_MODEL"],
        state={"question": RERANK_QUERY},
        questions=questions_payload(jev_questions()),
        api_key=keys["jev"],
        timeout=30.0,
    )


async def gemini_embedding(keys: dict[str, str]) -> Any:
    from app.gateway.backend import LiteLLMBackend
    from app.gateway.config import DEFAULT_MODELS
    from app.gateway.embeddings import DIMENSIONS

    return await LiteLLMBackend().embed(
        model="gemini/" + DEFAULT_MODELS["GEMINI_EMBEDDING_MODEL"],
        inputs=EMBED_INPUTS,
        api_key=keys["embedding"],
        dimensions=DIMENSIONS,
        timeout=30.0,
    )


async def openrouter_chat(keys: dict[str, str]) -> Any:
    from app.gateway.backend import LiteLLMBackend
    from app.gateway.config import DEFAULT_MODELS

    return await LiteLLMBackend().complete(
        model="openrouter/" + DEFAULT_MODELS["OPENROUTER_MODEL"],
        messages=CHAT_MESSAGES,
        api_key=keys["openrouter"],
        max_tokens=64,
        timeout=60.0,
    )


SCENARIOS: dict[str, Scenario] = {
    "cohere_rerank": cohere_rerank,
    "jina_rerank": jina_rerank,
    "jev_decisions": jev_decisions,
    "gemini_embedding": gemini_embedding,
    "openrouter_chat": openrouter_chat,
}
# the setting that holds the key of each scenario (names only; the recorder reads the values)
KEY_SETTINGS = {
    "cohere": "COHERE_API_KEY",
    "jina": "JINA_API_KEY",
    "jev": "JEV_OPENROUTER_API_KEY",
    "embedding": "GEMINI_EMBEDDING_KEY_A",
    "openrouter": "OPENROUTER_API_KEYS",  # a comma list: the first one is used
}
SCENARIO_KEY = {
    "cohere_rerank": "cohere",
    "jina_rerank": "jina",
    "jev_decisions": "jev",
    "gemini_embedding": "embedding",
    "openrouter_chat": "openrouter",
}
REPLAY_KEY = "replay-" + "k" * 12  # never a real key: the replay matches without the headers
