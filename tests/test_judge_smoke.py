"""The one-call judge check (scripts/judge_smoke.py): nothing happens without --run, it never
prints a key, it refuses on Render and it reports a failure by class and reason only."""

from pathlib import Path

import pytest

from app.gateway.errors import GatewayError
from scripts import judge_smoke

KEY = "sk-or-test-" + "q" * 24
ENV = {
    "EVAL_OPENROUTER_API_KEYS": KEY,
    "EVAL_JUDGE_MODEL": "meta-llama/llama-3.3-70b-instruct:free",
}


class Result:
    text = "OK"
    provider = "openrouter"
    model = "meta-llama/llama-3.3-70b-instruct:free"
    key_fingerprint = "ab12cd34"
    input_tokens = 12
    output_tokens = 2
    finish_reason = "stop"
    latency_s = 0.42


class FakeGateway:
    def __init__(self, config, *, result=None, error=None):
        self.config = config
        self.calls = []
        self._result, self._error = result or Result(), error

    async def complete_judge(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self._error:
            raise self._error
        return self._result


def run(argv, environ, capsys, **options):
    made = []

    def factory(config):
        gateway = FakeGateway(config, **options)
        made.append(gateway)
        return gateway

    # a path that does not exist: the tests never read the owner's own settings file
    missing = Path("no-such-settings-file")
    code = judge_smoke.main(argv, environ=dict(environ), env_path=missing, make_gateway=factory)
    return code, capsys.readouterr().out, made


def test_nothing_happens_without_run(capsys):
    code, out, made = run([], ENV, capsys)
    assert code == 0 and made == []
    assert "Nothing was called and no file was read" in out


def test_one_call_is_made_and_no_key_is_printed(capsys):
    code, out, made = run(["--run"], ENV, capsys)
    assert code == 0 and "the judge key works" in out
    assert len(made) == 1 and len(made[0].calls) == 1, "exactly one call"
    assert KEY not in out and "sk-or-test" not in out
    assert "key=ab12cd34" in out and "model=meta-llama/llama-3.3-70b-instruct:free" in out
    assert "input_tokens=12" in out and "finish=stop" in out and "seconds=0.42" in out


def test_the_call_goes_through_the_judge_path_with_a_fixed_trivial_prompt(capsys):
    code, _out, made = run(["--run"], ENV, capsys)
    messages, kwargs = made[0].calls[0]
    assert messages == judge_smoke.PROMPT and kwargs["max_tokens"] == 16
    assert code == 0


def test_a_missing_key_or_model_is_named_and_nothing_is_called(capsys):
    for environ, word in (
        ({"EVAL_GROQ_API_KEYS": "g" * 20, "EVAL_JUDGE_MODEL": ENV["EVAL_JUDGE_MODEL"]},
         "EVAL_OPENROUTER_API_KEYS"),
        ({"EVAL_OPENROUTER_API_KEYS": KEY}, "EVAL_JUDGE_MODEL"),
    ):  # fmt: skip
        code, out, made = run(["--run"], environ, capsys)
        assert code == 1 and word in out and made[0].calls == []
        assert KEY not in out


def test_an_empty_answer_is_reported_as_empty_and_fails(capsys):
    class Empty(Result):
        text = "   "

    code, out, _ = run(["--run"], ENV, capsys, result=Empty())
    assert code == 1 and "EMPTY" in out


def test_a_provider_failure_prints_the_class_and_reason_not_the_message(capsys):
    class Failed(GatewayError):
        pass

    # the gateway's errors carry a short reason CODE; the script prints the class and that code
    code, out, _ = run(["--run"], ENV, capsys, error=Failed("rate_limited"))
    assert code == 1 and "FAILED" in out and "error=Failed" in out and "reason=rate_limited" in out
    assert KEY not in out


def test_it_refuses_on_render(capsys):
    code, out, made = run(["--run"], {**ENV, "RENDER": "true"}, capsys)
    assert code == 1 and "refused" in out and made == []


def test_a_groq_judge_is_named_with_its_fallback_and_needs_no_openrouter_key(capsys):
    groq_only = {
        "EVAL_GROQ_API_KEYS": "g" * 20,
        "EVAL_JUDGE_GROQ_API_KEYS": "j" * 20,
        "EVAL_JUDGE_GROQ_MODEL": "qwen/qwen3.8-27b",
    }
    code, out, made = run(["--run"], groq_only, capsys)
    assert code == 0 and "judge model (Groq)" in out and "model=qwen/qwen3.8-27b" in out
    assert "fallback" not in out and len(made[0].calls) == 1
    both = {**groq_only, **ENV}
    code, out, _ = run(["--run"], both, capsys)
    assert code == 0 and "fallback model" in out and "j" * 20 not in out


@pytest.mark.parametrize("flag", ["--RUN", "run", "-run"])
def test_only_the_exact_flag_counts(flag, capsys):
    code, _out, made = run([flag], ENV, capsys)
    assert code == 0 and made == []


def test_a_stored_case_is_read_from_the_local_database_read_only(tmp_path):
    import json
    import sqlite3

    from scripts import ragas_smoke

    path = tmp_path / "eval.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE eval_cases (id TEXT, turns TEXT, expect TEXT)")
        db.execute(
            "CREATE TABLE eval_items (case_id TEXT, answer_kind TEXT, answer_text TEXT, "
            "contexts TEXT, finished_at TEXT)"
        )
        db.execute(
            "INSERT INTO eval_cases VALUES ('c1', ?, ?)",
            (json.dumps(["Q one?", "Q two?"]), json.dumps({"reference": "R"})),
        )
        db.execute(
            "INSERT INTO eval_items VALUES ('c1', 'answer', 'A', ?, '2026-10-21')",
            (json.dumps(["p1", "p2"]),),
        )
    case = ragas_smoke.stored_case("c1", path)
    assert case == {"question": "Q two?", "answer": "A", "contexts": ["p1", "p2"], "reference": "R"}
    assert ragas_smoke.stored_case("missing", path) is None
