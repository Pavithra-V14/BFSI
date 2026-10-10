"""The pipeline smoke script is off by default, reads .env only with --run and prints names, timings
and
counts: never the question, the answer, a citation's text or a key."""

import secrets

from scripts import pipeline_smoke
from tests.pipeline_rig import PipelineRig

QUESTION = "What is the waiting period for illness claims?"


def secret(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def test_without_run_nothing_is_called_and_no_file_is_read(capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not run without --run")

    monkeypatch.setattr(pipeline_smoke, "load_env_file", forbidden)
    monkeypatch.setattr(pipeline_smoke, "load_owner_env", forbidden)
    monkeypatch.setattr(pipeline_smoke, "real_setup", forbidden)
    assert pipeline_smoke.main([]) == 0
    out = capsys.readouterr().out
    assert "Nothing was called and no file was read" in out and "--run" in out


def test_with_run_it_prints_stage_names_and_counts_and_never_text_or_values(tmp_path, capsys):
    values = {
        "JEV_OPENROUTER_API_KEY": secret("jev"),
        "QDRANT_API_KEY": secret("qd"),
        "DATABASE_URL": "postgresql://user:" + secret("pw") + "@db.remote.test/db",
    }
    env = tmp_path / "sample.env"
    env.write_text("".join(f"{k}={v}\n" for k, v in values.items()), "utf-8")
    environ: dict[str, str] = {}
    holder = {}

    def setup(environ_, workdir, with_cache, role):
        (tmp_path / "rig").mkdir()
        rig = PipelineRig(tmp_path / "rig")
        holder["rig"] = rig
        return rig.deps, rig.db, rig.new_user(role)

    code = pipeline_smoke.main(
        ["--run", "--question", QUESTION], env_path=env, environ=environ, make_setup=setup
    )
    out = capsys.readouterr().out
    assert code == 0
    for stage in (
        "authenticate",
        "local_scan",
        "triage",
        "embed",
        "retrieve",
        "rerank",
        "generate",
    ):
        assert stage in out
    assert "RESULT" in out and "ANSWER" in out and "citations=1" in out and "key=fp456" in out
    assert (
        "loaded 2 variable(s) from .env" in out
    )  # the gateway key and the qdrant key; not DATABASE_URL
    assert "DATABASE_URL" not in environ and "DATABASE_URL" not in out
    for value in values.values():
        assert value not in out  # values are never shown
    assert "waiting period" not in out.lower()  # neither the question nor the answer
    assert "30 days" not in out and "clause" not in out.lower()
    holder["rig"].close()


def test_the_line_printer_hides_anything_that_looks_like_text(capsys):
    pipeline_smoke.line("stage", "OK", ms=3, model="m-1", sentence="What is my claim status today?")
    out = capsys.readouterr().out
    assert "ms=3" in out and "model=m-1" in out and "<hidden>" in out and "claim status" not in out
