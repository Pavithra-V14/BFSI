"""The judge on Groq with its own key, the OpenRouter judge as its fallback ([P], owner request).

Rules guarded here: the Groq judge is tried first and only when it has both a key and a model; when
it fails the OpenRouter judge answers; it uses its own key pool, never the answer keys; it is
refused on Render like every evaluation key; its model must be of another family than the answer
models; the estimate counts the judge's daily limits from the judge that answers first."""

import pytest

from app.eval.estimate import daily_limits
from app.gateway.config import GatewayConfig, effective_judge_model
from app.gateway.errors import AllProvidersFailed, ProviderUnavailable
from app.gateway.keys import ConfigError
from tests.helpers import FakeBackend, fake_key, make_config, make_gateway, ok, run

GROQ_JUDGE = "qwen/qwen3.8-27b"
OR_JUDGE = "nvidia/nemotron-3-super-120b-a12b:free"
JUDGE_KEY = fake_key("judge-groq")
MESSAGES = [{"role": "user", "content": "hello there"}]


def groq_judge_config(**overrides):
    values = {
        "eval_judge_groq_keys": [JUDGE_KEY],
        "eval_judge_groq_model": GROQ_JUDGE,
        "eval_judge_model": OR_JUDGE,
    }
    values.update(overrides)
    return make_config(**values)


# ---- the chain ------------------------------------------------------------------------------
def test_groq_judges_first_and_openrouter_is_the_fallback():
    steps = make_gateway(groq_judge_config()).judge_steps()
    assert [(s.provider, s.model, s.pool_name) for s in steps] == [
        ("groq", GROQ_JUDGE, "eval_judge_groq"),
        ("openrouter", OR_JUDGE, "eval_openrouter"),
    ]


def test_without_a_groq_key_only_the_openrouter_judge_remains():
    config = make_config(eval_judge_groq_model=GROQ_JUDGE, eval_judge_model=OR_JUDGE)
    steps = make_gateway(config).judge_steps()
    assert [s.provider for s in steps] == ["openrouter"]


def test_with_only_a_groq_judge_there_is_no_openrouter_step():
    steps = make_gateway(groq_judge_config(eval_judge_model=None)).judge_steps()
    assert [s.provider for s in steps] == ["groq"]


def test_the_groq_judge_answers_on_its_own_key():
    backend = FakeBackend(lambda model, key, n: ok("from groq"))
    gateway = make_gateway(groq_judge_config(), backend=backend)
    done = run(gateway.complete_judge(MESSAGES))
    assert done.text == "from groq" and done.provider == "groq"
    assert backend.calls == [("groq/" + GROQ_JUDGE, JUDGE_KEY)]  # never an answer key


def test_when_groq_fails_openrouter_judges():
    def handler(model, key, n):
        return ProviderUnavailable() if model.startswith("groq/") else ok("from openrouter")

    backend = FakeBackend(handler)
    gateway = make_gateway(groq_judge_config(), backend=backend)
    done = run(gateway.complete_judge(MESSAGES))
    assert done.text == "from openrouter" and done.provider == "openrouter"
    assert done.skipped and done.skipped[0][0] == "groq"
    assert [m.split("/")[0] for m, _ in backend.calls][:1] == ["groq"]


def test_when_both_fail_the_answer_is_all_providers_failed():
    backend = FakeBackend(lambda model, key, n: ProviderUnavailable())
    gateway = make_gateway(groq_judge_config(), backend=backend)
    with pytest.raises(AllProvidersFailed) as caught:
        run(gateway.complete_judge(MESSAGES))
    assert {provider for provider, _ in caught.value.skipped} == {"groq", "openrouter"}


# ---- the settings ---------------------------------------------------------------------------
def test_the_settings_are_read_from_the_environment():
    env = {
        "EVAL_GROQ_API_KEYS": fake_key("evalgroq"),
        "EVAL_JUDGE_GROQ_API_KEYS": f"{JUDGE_KEY}, {fake_key('judge-groq', 2)}",
        "EVAL_JUDGE_GROQ_MODEL": GROQ_JUDGE,
        "EVAL_JUDGE_MODEL": OR_JUDGE,
    }
    config = GatewayConfig.for_evaluation(env)
    assert config.eval_judge_groq_keys == [JUDGE_KEY, fake_key("judge-groq", 2)]
    assert config.eval_judge_groq_model == GROQ_JUDGE
    assert effective_judge_model(config) == GROQ_JUDGE


def test_the_judge_model_name_follows_the_judge_that_answers_first():
    assert effective_judge_model(groq_judge_config()) == GROQ_JUDGE
    assert effective_judge_model(make_config(eval_judge_model=OR_JUDGE)) == OR_JUDGE
    assert effective_judge_model(type("C", (), {"eval_judge_model": OR_JUDGE})()) == OR_JUDGE
    assert effective_judge_model(make_config()) is None


def test_a_judge_key_without_a_judge_model_is_refused():
    with pytest.raises(ConfigError) as caught:
        make_config(eval_judge_groq_keys=[JUDGE_KEY])
    assert "EVAL_JUDGE_GROQ_MODEL" in str(caught.value) and JUDGE_KEY not in str(caught.value)


def test_the_groq_judge_must_not_be_of_an_answer_family():
    with pytest.raises(ConfigError) as caught:
        groq_judge_config(eval_judge_groq_model="openai/gpt-oss-120b")
    assert "EVAL_JUDGE_GROQ_MODEL" in str(caught.value)


def test_the_judge_key_cannot_also_be_an_answer_key():
    with pytest.raises(ConfigError):
        groq_judge_config(eval_judge_groq_keys=[fake_key("groq", 1)])  # the same as an answer key


def test_the_judge_key_is_never_shown_in_the_config_text():
    assert JUDGE_KEY not in repr(groq_judge_config())


# ---- never on Render ------------------------------------------------------------------------
def test_the_groq_judge_key_is_refused_on_render():
    with pytest.raises(ConfigError) as caught:
        GatewayConfig.from_env({"EVAL_JUDGE_GROQ_API_KEYS": JUDGE_KEY, "RENDER": "true"})
    assert "EVAL_JUDGE_GROQ_API_KEYS" in str(caught.value) and JUDGE_KEY not in str(caught.value)


def test_a_config_holding_the_groq_judge_key_is_refused_on_render():
    with pytest.raises(ConfigError):
        make_gateway(groq_judge_config(), env={"RENDER": "1"})


# ---- the estimate ---------------------------------------------------------------------------
def test_the_estimate_takes_the_judges_limits_from_groq_when_groq_judges():
    groq = daily_limits(groq_judge_config())
    other = daily_limits(make_config(eval_judge_model=OR_JUDGE))
    assert groq.keys["judge"] == 1
    assert groq.requests["judge"] != other.requests["judge"]  # OpenRouter's 50 a day no longer
