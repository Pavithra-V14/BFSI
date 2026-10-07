"""Image captions through the gateway (spec 6.1): Gemini only, same pool, limiter and breaker."""

import base64
import logging

import pytest

from app.gateway.config import DEFAULT_LIMITS
from app.gateway.errors import AllProvidersFailed
from app.gateway.gateway import IMAGE_TOKEN_ESTIMATE, MAX_IMAGE_BYTES
from app.gateway.limiter import Limits
from app.guardrails.prompts import get_prompt
from tests.helpers import (
    MESSAGES,
    FakeBackend,
    fake_key,
    lit,
    make_config,
    make_gateway,
    ok,
    run,
)

CAPTION_PROMPT = get_prompt("caption").render()
PICTURE = b"\x89PNG-this-is-a-picture-" + bytes(range(60))
CAPTION = "A bar chart of premium by year, rising from 100 to 140."


class CapturingBackend(FakeBackend):
    def __init__(self, handler=None):
        super().__init__(handler)
        self.messages = []

    async def complete(self, *, messages, **kwargs):
        self.messages.append(messages)
        return await super().complete(messages=messages, **kwargs)


def captioner(text=CAPTION):
    return CapturingBackend(lambda model, key, n: ok(text))


def test_a_caption_comes_from_gemini_and_carries_the_image_as_a_data_url():
    backend = captioner()
    result = run(make_gateway(backend=backend).caption_image(PICTURE, prompt=CAPTION_PROMPT))
    assert result.text == CAPTION and result.decider == "gemini"
    assert backend.calls == [("gemini/gemini-3.8-flash", fake_key("gemini"))]  # no other provider
    [content] = [m["content"] for m in backend.messages[0]]
    assert content[0] == {"type": "text", "text": CAPTION_PROMPT}
    url = content[1]["image_url"]["url"]
    assert url == "data:image/png;base64," + base64.b64encode(PICTURE).decode()
    assert result.input_tokens == 10 and result.cost_billed == 0.0  # the pilot is on a free plan


def test_neither_the_image_nor_the_caption_reaches_an_event_or_a_log(caplog):
    caplog.set_level(logging.DEBUG)
    backend = captioner()
    gateway = make_gateway(backend=backend)
    run(gateway.caption_image(PICTURE, prompt=CAPTION_PROMPT))
    failing = make_gateway(backend=FakeBackend(lambda m, k, n: lit("ServiceUnavailableError")))
    with pytest.raises(AllProvidersFailed):
        run(failing.caption_image(PICTURE, prompt=CAPTION_PROMPT))
    everything = repr(gateway.events) + repr(failing.events) + caplog.text + repr(gateway.stats)
    assert base64.b64encode(PICTURE).decode()[:20] not in everything
    assert "PNG-this-is-a-picture" not in everything and "bar chart" not in everything


def test_it_is_gemini_only_and_a_failure_does_not_fall_back_to_another_provider():
    backend = CapturingBackend(lambda model, key, n: lit("ServiceUnavailableError"))
    with pytest.raises(AllProvidersFailed) as failed:
        run(make_gateway(backend=backend).caption_image(PICTURE, prompt=CAPTION_PROMPT))
    assert {model.split("/")[0] for model, _ in backend.calls} == {"gemini"}
    assert [p for p, _ in failed.value.skipped] == ["gemini"]


def test_it_uses_the_gemini_key_pool_and_its_limiter():
    limits = {**DEFAULT_LIMITS, "GEMINI": Limits(rpm=1)}
    gateway = make_gateway(make_config(limits=limits), backend=captioner())
    run(gateway.caption_image(PICTURE, prompt=CAPTION_PROMPT))
    with pytest.raises(AllProvidersFailed) as failed:  # the one request this minute is spent
        run(gateway.caption_image(PICTURE, prompt=CAPTION_PROMPT))
    assert failed.value.skipped == [("gemini", "limiter_full")]


def test_a_caption_call_counts_against_gemini_answers_too():
    limits = {**DEFAULT_LIMITS, "GEMINI": Limits(rpm=1)}
    gateway = make_gateway(make_config(limits=limits), backend=captioner())
    run(gateway.caption_image(PICTURE, prompt=CAPTION_PROMPT))
    with pytest.raises(AllProvidersFailed):  # the one Gemini request this minute is spent
        run(gateway.complete(MESSAGES, only_provider="gemini"))


def test_it_shares_the_breaker_with_gemini_answers():
    backend = CapturingBackend(lambda model, key, n: lit("RateLimitError"))
    gateway = make_gateway(backend=backend)
    with pytest.raises(AllProvidersFailed):
        run(gateway.caption_image(PICTURE, prompt=CAPTION_PROMPT))
    assert gateway.health()["gemini"]["state"] == "circuit_open"
    calls = len(backend.calls)
    with pytest.raises(AllProvidersFailed) as failed:
        run(gateway.caption_image(PICTURE, prompt=CAPTION_PROMPT))
    assert failed.value.skipped == [("gemini", "circuit_open")] and len(backend.calls) == calls


def test_no_gemini_key_is_a_clear_failure_not_a_crash():
    gateway = make_gateway(make_config(gemini_keys=[]), backend=captioner())
    with pytest.raises(AllProvidersFailed) as failed:
        run(gateway.caption_image(PICTURE, prompt=CAPTION_PROMPT))
    assert failed.value.skipped == [("gemini", "no_keys")]


@pytest.mark.parametrize(
    ("image", "mime"),
    [
        pytest.param(b"", "image/png", id="empty"),
        pytest.param(b"x" * (MAX_IMAGE_BYTES + 1), "image/png", id="too-large"),
        pytest.param(PICTURE, "application/pdf", id="pdf"),
        pytest.param(PICTURE, "image/svg+xml", id="svg"),
    ],
)
def test_a_bad_image_is_refused_before_any_call(image, mime):
    backend = captioner()
    with pytest.raises(ValueError):
        run(make_gateway(backend=backend).caption_image(image, mime, prompt=CAPTION_PROMPT))
    assert backend.calls == []


def test_the_limiter_is_charged_for_the_picture_not_for_its_base64_text():
    # A picture of 1 MB would be a million "tokens" if its text were counted; it must not be.
    big = b"\x00" * 1_000_000
    gateway = make_gateway(backend=captioner())
    assert run(gateway.caption_image(big, prompt=CAPTION_PROMPT)).text == CAPTION
    assert IMAGE_TOKEN_ESTIMATE < 5_000
