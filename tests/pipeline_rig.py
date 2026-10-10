"""Shared fakes for the pipeline tests: a scripted gateway, an ingested in-memory Qdrant, fakeredis,
SQLite and a masker that needs no model. Nothing here touches a network or a real key."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.gateway.decisions import ChoiceAnswer, DecisionResult, NoulAnswer
from app.gateway.errors import AllProvidersFailed, EmbeddingUnavailable
from app.gateway.gateway import CompletionResult, EmbeddingResult
from app.gateway.rerank import RerankItem, RerankResult, lexical_scores, top_items
from app.guardrails.domains import curated_domains
from app.guardrails.masking import Masker
from app.pipeline.run import Pipeline
from app.pipeline.startup import MaskerHolder
from app.pipeline.state import Deps, Request
from tests.helpers import WordCounter
from tests.ingest_fixtures import FakeEmbedder, Rig, para, parsed, title

START = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


class Quiet:
    """A Presidio stand-in that finds nothing; the regex layer of the masker still runs."""

    def analyze(self, **kwargs):
        return []


class NameAnalyzer:
    """A Presidio stand-in that finds the given names as PERSON (the real model is not needed)."""

    def __init__(self, names):
        self.names = list(names)

    def analyze(self, *, text, **kwargs):
        from types import SimpleNamespace

        found = []
        for name in self.names:
            start = text.find(name)
            while start != -1:
                found.append(
                    SimpleNamespace(
                        entity_type="PERSON", start=start, end=start + len(name), score=0.9
                    )
                )
                start = text.find(name, start + len(name))
        return found


def masker_holder() -> MaskerHolder:
    return MaskerHolder.ready_with(Masker(analyzer=Quiet()))


def waiting_period_document():
    return parsed(
        title("Section 1. Definitions"),
        para("Policy means this contract. Insured means the person covered."),
        title("Section 2. Waiting periods"),
        para("A waiting period of 30 days applies to illness claims."),
        title("Section 3. Exclusions"),
        para("War and nuclear risks are excluded. Self harm is excluded."),
    )


@dataclass
class FakeGateway:
    """Scripted answers; every call is recorded (kind, tier, small facts, never needing text)."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    in_domain: float | None = 0.95
    needs_history: float = 0.05
    vague: float = 0.05
    injection: float | None = 0.02
    product_line: tuple[str, float] | None = None
    clause_type: tuple[str, float] | None = None
    decide_error: Exception | None = None
    decider: str = "jev"
    rewrite_reply: Any = None  # a dict, or a callable(prompt) -> dict / str / Exception
    answer_reply: Any = None  # a dict {"answer":..., "citations": [...]}, str, callable, list
    summary_reply: str = "The customer asked about waiting periods."
    complete_error: Exception | None = None
    embed_error: Exception | None = None
    rerank_error: Exception | None = None
    rerank_kind: str = "cohere"
    rerank_scores: Any = None  # a list of scores, or callable(query, docs) -> list
    delay: dict[str, float] = field(default_factory=dict)
    answers_given: list[Any] = field(default_factory=list)
    embedder: FakeEmbedder = field(default_factory=FakeEmbedder)

    def log(self, kind: str, **facts: Any) -> None:
        self.calls.append({"kind": kind, **facts})

    async def _wait(self, kind: str) -> None:
        if self.delay.get(kind):
            await asyncio.sleep(self.delay[kind])

    async def decide(self, questions, state, *, tier="decide", timeout_s=None, max_wait_s=0.0):
        self.log(
            "decide",
            tier=tier,
            ids=[q.id for q in questions],
            options={q.id: list(q.options) for q in questions if hasattr(q, "options")},
            text=state,
            timeout_s=timeout_s,
        )
        await self._wait("decide")
        if self.decide_error:
            raise self.decide_error
        if self.decider == "rules":
            return DecisionResult(None, "rules")
        answers: dict[str, Any] = {}
        for question in questions:
            if question.id == "domain_relevance" and self.in_domain is not None:
                answers[question.id] = NoulAnswer(self.in_domain)
            elif question.id == "needs_history":
                answers[question.id] = NoulAnswer(self.needs_history)
            elif question.id == "vague_first_message":
                answers[question.id] = NoulAnswer(self.vague)
            elif question.id == "injection_attempt" and self.injection is not None:
                answers[question.id] = NoulAnswer(self.injection)
            elif question.id == "product_line" and self.product_line:
                option, probability = self.product_line
                rest = {
                    o: (1 - probability) / max(len(question.options) - 1, 1)
                    for o in question.options
                }
                rest[option] = probability
                answers[question.id] = ChoiceAnswer(option, rest, probability)
            elif question.id == "clause_type" and self.clause_type:
                option, probability = self.clause_type
                rest = {
                    o: (1 - probability) / max(len(question.options) - 1, 1)
                    for o in question.options
                }
                rest[option] = probability
                answers[question.id] = ChoiceAnswer(option, rest, probability)
        return DecisionResult(
            answers,
            self.decider,
            model="m",
            provider="p",
            key_fingerprint="fp123",
            input_tokens=20,
            output_tokens=5,
        )

    async def complete(self, messages, *, tier="answer", max_tokens=None, json_mode=False,
                       timeout_s=None, only_provider=None, max_wait_s=0.0):  # fmt: skip
        prompt = messages[0]["content"]
        if "LATEST MESSAGE" in prompt:
            kind = "rewrite"
        elif "OLD TURNS" in prompt:
            kind = "summary"
        else:
            kind = "answer"
        self.log("complete", what=kind, tier=tier, prompt=prompt, timeout_s=timeout_s)
        await self._wait(kind)
        if self.complete_error:
            raise self.complete_error
        reply = {
            "rewrite": self.rewrite_reply,
            "summary": self.summary_reply,
            "answer": self.answer_reply,
        }[kind]
        if kind == "answer" and reply is None:
            reply = {"answer": "A waiting period of 30 days applies to illness claims.",
                     "citations": [{"chunk_id": "S1"}]}  # fmt: skip
        if kind == "rewrite" and reply is None:
            reply = {
                "standalone_question": "What is the waiting period for illness claims?",
                "clear": True,
                "understood": "waiting period",
                "clarifying_question": "",
            }
        if callable(reply):
            reply = reply(prompt)
        if isinstance(reply, list):  # one reply per call
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        self.answers_given.append(text)
        return CompletionResult(text, "groq", "model-x", "fp456", provider="groq",
                                input_tokens=100, output_tokens=40)  # fmt: skip

    async def embed(self, texts, *, task="document", titles=None, timeout_s=None, max_wait_s=0.0):
        self.log("embed", task=task, count=len(texts), text=texts[0], timeout_s=timeout_s)
        await self._wait("embed")
        if self.embed_error:
            raise self.embed_error
        vectors = [self.embedder.vector(t) for t in texts]
        return EmbeddingResult(vectors, "embed-model", key_fingerprint="fpE", input_tokens=10)

    async def rerank(self, query, documents, *, top_n=None, timeout_s=None, max_wait_s=0.0):
        self.log("rerank", count=len(documents), query=query, timeout_s=timeout_s)
        await self._wait("rerank")
        if self.rerank_error:
            raise self.rerank_error
        if not documents:
            return RerankResult([], "none")
        if callable(self.rerank_scores):
            scores = self.rerank_scores(query, documents)
        elif self.rerank_scores is not None:
            scores = list(self.rerank_scores)[: len(documents)]
        elif self.rerank_kind == "lexical":
            scores = lexical_scores(query, documents)
        else:
            scores = [0.9 - 0.05 * i for i in range(len(documents))]
        items = top_items(scores, top_n)
        return RerankResult(items, self.rerank_kind, model="rr", key_fingerprint="fpR",
                            degraded=self.rerank_kind == "lexical", input_tokens=30)  # fmt: skip

    def calls_of(self, kind: str, **match: Any) -> list[dict[str, Any]]:
        return [
            c
            for c in self.calls
            if c["kind"] == kind and all(c.get(k) == v for k, v in match.items())
        ]

    def model_calls(self) -> int:
        return len(
            [c for c in self.calls if c["kind"] in ("decide", "complete", "embed", "rerank")]
        )


class PipelineRig:
    """One pipeline over a seeded corpus, with every dependency a local fake."""

    def __init__(self, tmp_path, *, documents=None, gateway: FakeGateway | None = None, roles=None):
        self.rig = Rig(tmp_path)
        self.gateway = gateway or FakeGateway()
        self.gateway.embedder = self.rig.embedder
        docs = documents or [("doc-a", "customer", waiting_period_document())]
        for doc_id, role, document, *rest in docs:
            self.rig.ingest(document, doc_id=doc_id, access_role=role, **(rest[0] if rest else {}))
        self.events: list[tuple[str, dict]] = []
        self.spans: list[Any] = []
        self.deps = Deps(
            gateway=self.gateway,
            qdrant=self.rig.qdrant,
            cache=self.rig.cache,
            masker=masker_holder(),
            counter=WordCounter(),
            usage=None,
            curated_domains=curated_domains(),
            on_event=lambda name, fields: self.events.append((name, fields)),
            on_span=self.spans.append,
            now=lambda: START,
        )
        self.pipeline = Pipeline(self.deps)
        self.db = self.rig.db

    @property
    def event_names(self) -> list[str]:
        return [name for name, _ in self.events]

    def new_user(self, role: str = "customer") -> str:
        user_id = str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO users (id, username, email, password_hash, role) VALUES (?, ?, ?, ?, ?)",
            (user_id, f"u{user_id[:8]}", f"{user_id[:8]}@example.test", "h", role),
        )
        self.users = getattr(self, "users", {})
        self.users[user_id] = role
        return user_id

    async def ask(self, query: str, *, user: str | None = None, role: str = "customer",
                  session_id: str | None = None):  # fmt: skip
        user = user or self.new_user(role)
        request = Request(user_id=user, role=role, query=query, session_id=session_id)
        return await self.pipeline.answer(request, self.db)

    def close(self) -> None:
        self.rig.close()


def later(minutes: int):
    return START + timedelta(minutes=minutes)


__all__ = [
    "AllProvidersFailed",
    "EmbeddingUnavailable",
    "FakeGateway",
    "PipelineRig",
    "RerankItem",
    "START",
    "later",
    "waiting_period_document",
]
