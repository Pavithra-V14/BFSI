"""The smoke script is off by default, reads .env only with --run, and prints names and status."""

import secrets

from app.gateway.errors import CreditExhausted
from scripts import gateway_smoke
from tests.helpers import FakeBackend, FakeDecisionClient, fake_key, make_gateway, run
from tests.test_gateway_decisions import jev_response


def secret(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def env_file(tmp_path, values: dict[str, str]):
    path = tmp_path / "sample.env"
    path.write_text("".join(f"{name}={value}\n" for name, value in values.items()), "utf-8")
    return path


def fake_factory(decision_handler=None):
    client = FakeDecisionClient(decision_handler or (lambda call, n: jev_response()))
    return lambda config: make_gateway(config, backend=FakeBackend(), decision_client=client)


def test_without_run_nothing_is_called_and_no_file_is_read(capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the .env loader must not run without --run")

    monkeypatch.setattr(gateway_smoke, "load_env_file", forbidden)
    monkeypatch.setattr(gateway_smoke, "load_owner_env", forbidden)
    assert gateway_smoke.main([]) == 0
    out = capsys.readouterr().out
    assert "Nothing was called and no file was read" in out and "--run" in out


def test_with_run_it_loads_gateway_names_only_and_prints_no_value(tmp_path, capsys):
    values = {
        "JEV_OPENROUTER_API_KEY": secret("jev"),
        "GEMINI_EMBEDDING_KEY_A": secret("embed"),
        "GEMINI_API_KEYS": secret("gemini"),
        "DATABASE_URL": "postgresql://user:"
        + secret("pw")
        + "@db.remote.test/db",  # not a gateway name
    }
    path = env_file(tmp_path, values)
    environ: dict[str, str] = {}
    code = gateway_smoke.main(
        ["--run"], env_path=path, environ=environ, make_gateway=fake_factory()
    )
    out = capsys.readouterr().out
    assert code == 0 and out.count(" OK") == 3
    assert "loaded 3 variable(s) from .env" in out
    for name in ("JEV_OPENROUTER_API_KEY", "GEMINI_EMBEDDING_KEY_A", "GEMINI_API_KEYS"):
        assert name in out  # names are shown
    for value in values.values():
        assert value not in out  # values never are
    assert "DATABASE_URL" not in environ  # only gateway variables are loaded
    assert "snapshot=typesafe/jev-1.13-20260917" in out


def test_with_run_and_no_env_file_it_says_so_and_skips_missing_keys(tmp_path, capsys):
    missing = tmp_path / "does-not-exist.env"
    code = gateway_smoke.main(["--run"], env_path=missing, environ={}, make_gateway=fake_factory())
    out = capsys.readouterr().out
    assert code == 0 and "no .env file found" in out
    assert out.count("SKIPPED") == 5 and " OK" not in out  # jev, embedding, three text calls


def test_a_shell_variable_wins_over_the_file(tmp_path, capsys):
    from_file, from_shell = secret("file"), secret("shell")
    path = env_file(tmp_path, {"GEMINI_API_KEYS": from_file})
    environ = {"GEMINI_API_KEYS": from_shell}
    gateway_smoke.main(["--run"], env_path=path, environ=environ, make_gateway=fake_factory())
    assert environ["GEMINI_API_KEYS"] == from_shell
    assert from_file not in capsys.readouterr().out


def test_every_call_prints_names_and_status_but_no_key_or_text(capsys):
    backend = FakeBackend()
    client = FakeDecisionClient(lambda call, n: jev_response())
    gateway = make_gateway(backend=backend, decision_client=client)
    assert run(gateway_smoke.smoke(gateway)) == 0
    out = capsys.readouterr().out
    assert out.count(" OK") == 5  # jev, embedding, and one text call each to three providers
    for label in ("gemini text call", "groq text call", "openrouter text call"):
        assert label in out
    assert "text_non_empty=True" in out and "finish_reason=stop" in out
    assert "reasoning_tokens=" in out and "output_tokens=5" in out
    assert sorted(c[0].split("/")[0] for c in backend.calls) == ["gemini", "groq", "openrouter"]
    assert all(r["max_tokens"] == 912 for r in backend.requests)  # the real answer limit + room
    assert "snapshot=typesafe/jev-1.13-20260917" in out and "length=768" in out
    assert "norm=1.0" in out and "calibrated=True" in out
    for name in ("groq", "gemini", "openrouter", "jev", "embedA", "embedB", "cohere", "jina"):
        assert fake_key(name) not in out
    assert "hello" not in out  # the model's text is never printed


def test_an_empty_answer_is_reported_as_a_problem_not_as_ok(capsys):
    from tests.helpers import ok

    empty = ok("", finish_reason="length", completion=400, reasoning_tokens=400)
    gateway = make_gateway(
        backend=FakeBackend(lambda m, k, n: empty),
        decision_client=FakeDecisionClient(lambda call, n: jev_response()),
    )
    assert run(gateway_smoke.smoke(gateway)) == 3  # all three text calls came back empty
    out = capsys.readouterr().out
    assert out.count("FAILED") == 3 and "truncated" in out and "text_non_empty" not in out


def test_a_jev_fallback_is_reported_not_hidden(capsys):
    client = FakeDecisionClient(lambda call, n: CreditExhausted(status=402))
    gateway = make_gateway(backend=FakeBackend(), decision_client=client)
    assert run(gateway_smoke.smoke(gateway)) == 1
    out = capsys.readouterr().out
    assert "FELL BACK" in out and "credit_exhausted" in out
