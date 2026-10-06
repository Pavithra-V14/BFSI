"""The real LiteLLM backend, run offline with LiteLLM's own mock_response (no network, no key)."""

import litellm
import pytest

from app.gateway.backend import LiteLLMBackend
from tests.helpers import MESSAGES, fake_key, run


@pytest.fixture
def mocked_completion(monkeypatch):
    original = litellm.acompletion
    seen = {}

    async def with_mock(**kwargs):
        seen.update(kwargs)
        return await original(mock_response="a mocked answer", **kwargs)

    monkeypatch.setattr(litellm, "acompletion", with_mock)
    return seen


def complete(model: str, **extra):
    return run(
        LiteLLMBackend().complete(
            model=model,
            messages=MESSAGES,
            api_key=fake_key("groq"),
            max_tokens=20,
            timeout=5,
            **extra,
        )
    )


def test_the_backend_passes_the_expected_arguments_and_reads_the_response(mocked_completion):
    raw = complete("groq/openai/gpt-oss-120b")
    assert raw.text == "a mocked answer" and raw.finish_reason == "stop"
    assert raw.prompt_tokens and raw.completion_tokens
    assert mocked_completion["api_key"] == fake_key("groq")  # keys are passed per call
    assert mocked_completion["num_retries"] == 0 and mocked_completion["drop_params"] is True
    assert mocked_completion["max_tokens"] == 20 and mocked_completion["timeout"] == 5
    assert "response_format" not in mocked_completion
    assert raw.cost_source == "litellm"
    prices = litellm.model_cost["groq/openai/gpt-oss-120b"]  # the bundled map, no network
    expected = (
        raw.prompt_tokens * prices["input_cost_per_token"]
        + raw.completion_tokens * prices["output_cost_per_token"]
    )
    assert raw.cost_usd == pytest.approx(expected, rel=1e-9) and expected > 0


def test_response_format_is_passed_through_when_given(mocked_completion):
    complete("groq/openai/gpt-oss-120b", response_format={"type": "json_object"})
    assert mocked_completion["response_format"] == {"type": "json_object"}


def test_a_free_model_is_billed_zero_with_a_label(mocked_completion):
    raw = complete("openrouter/google/gemma-4-31b-it:free")
    assert raw.cost_usd == 0.0 and raw.cost_source == "free_model"


def test_a_model_litellm_cannot_price_has_unknown_cost_not_a_crash(mocked_completion):
    raw = complete("groq/a-model-that-litellm-does-not-know")
    assert raw.text == "a mocked answer"
    assert (raw.cost_usd, raw.cost_source) == (None, "unknown")  # exact: no guess at a price


def test_litellm_knows_every_model_the_gateway_uses():
    litellm_map = litellm.model_cost
    for model_id in (
        "groq/openai/gpt-oss-120b",
        "groq/openai/gpt-oss-20b",
        "gemini/gemini-3.8-flash",
        "openrouter/google/gemma-4-31b-it:free",
        "gemini/gemini-embedding-2",
    ):
        assert model_id in litellm_map, model_id
