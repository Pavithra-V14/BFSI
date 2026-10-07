"""The model calls of ingestion, with a fake gateway (nothing real is called)."""

from dataclasses import dataclass

import pytest

from app.gateway.decisions import ChoiceAnswer, NoulAnswer
from app.ingest.cache import DiskCache
from app.ingest.llm import LABEL_MIN_CONFIDENCE, SUMMARY_MAX_CHARS, LlmTools, sniff_mime
from app.ingest.runner import LoopRunner

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20


@dataclass
class Text:
    text: str


@dataclass
class Decision:
    answers: dict | None


class FakeGateway:
    def __init__(self):
        self.requests = []
        self.answers = None
        self.completion = "Limits by plan. More words."

    async def caption_image(self, image, mime, *, max_wait_s):
        self.requests.append(("caption", mime, max_wait_s))
        return Text("  A   chart\nof things ")

    async def complete(self, messages, *, tier, max_tokens, max_wait_s):
        self.requests.append(("complete", tier, max_tokens, messages[0]["content"]))
        return Text(self.completion)

    async def decide(self, questions, state, *, tier, max_wait_s):
        self.requests.append(("decide", questions[0].id, tier, state))
        return Decision(self.answers)


@pytest.fixture
def tools():
    runner = LoopRunner()
    gateway = FakeGateway()
    yield LlmTools(gateway, runner), gateway
    runner.close()


def test_a_caption_goes_to_the_gateways_caption_call_with_the_sniffed_type(tools):
    llm, gateway = tools
    assert llm.caption(PNG) == "A chart of things"
    assert gateway.requests == [("caption", "image/png", 90.0)]


def test_the_image_type_is_sniffed_from_the_bytes_not_the_name():
    assert sniff_mime(PNG) == "image/png"
    assert sniff_mime(b"\xff\xd8\xff\xe0 jpeg") == "image/jpeg"
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    with pytest.raises(ValueError):
        sniff_mime(b"%PDF-1.4")


def test_a_table_summary_uses_the_decide_tier_and_is_one_clipped_line(tools):
    llm, gateway = tools
    gateway.completion = "x" * 1000
    summary = llm.summarise("a | b")
    assert len(summary) == SUMMARY_MAX_CHARS
    kind, tier, max_tokens, prompt = gateway.requests[0]
    assert (kind, tier, max_tokens) == ("complete", "decide", 80) and prompt.endswith("a | b")


def test_a_label_is_used_only_when_the_decider_is_sure_enough(tools):
    llm, gateway = tools
    gateway.answers = {"clause_label": ChoiceAnswer("claims", {"claims": 0.9}, 0.9)}
    assert llm.label("how to claim") == "claims"
    gateway.answers = {"clause_label": ChoiceAnswer("claims", {}, LABEL_MIN_CONFIDENCE - 0.01)}
    assert llm.label("vague") == "general"
    gateway.answers = {"clause_label": ChoiceAnswer("not-a-label", {}, 0.99)}
    assert llm.label("odd") == "general"
    gateway.answers = None  # no decider answered: the rules say "general"
    assert llm.label("anything") == "general"
    assert all(r[:3] == ("decide", "clause_label", "decide") for r in gateway.requests)


def test_the_entity_question_returns_a_probability_or_none(tools):
    llm, gateway = tools
    gateway.answers = {"entity_candidate": NoulAnswer(0.93)}
    assert llm.classify("IRDAI", "context text") == 0.93
    assert "Highlighted text: IRDAI" in gateway.requests[0][3]
    gateway.answers = None
    assert llm.classify("IRDAI", "context") is None


def test_the_text_sent_to_a_decider_is_clipped(tools):
    llm, gateway = tools
    llm.label("w " * 10_000)
    assert len(gateway.requests[0][3]) <= 2_500


def test_the_disk_cache_keys_by_what_was_sent_and_survives_a_missing_folder(tmp_path):
    cache = DiskCache(tmp_path / "c")
    assert cache.get("label", "text") is None
    cache.put("label", "text", "claims")
    assert cache.get("label", "text") == "claims"
    assert cache.get("label", "other text") is None and cache.get("summary", "text") is None
    off = DiskCache(None)
    off.put("label", "text", "x")
    assert off.get("label", "text") is None


def test_a_cache_that_cannot_write_is_only_slower(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    cache = DiskCache(blocker / "inside")
    cache.put("label", "t", "v")  # no error
    assert cache.get("label", "t") is None
