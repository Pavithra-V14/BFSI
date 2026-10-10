"""`pipeline_smoke.py --langfuse` sends one trace of span records to YOUR Langfuse and prints only
SENT, or the exception type that stopped it: never a key, a host, a message or any text."""

import pytest

from app.pipeline import langfuse_sink
from app.pipeline.langfuse_sink import LangfuseSink
from scripts import pipeline_smoke
from tests.pipeline_rig import PipelineRig
from tests.test_pipeline_langfuse import (
    AUDIT_KEY,
    HOST,
    JWT_KEY,
    PUBLIC,
    SECRET,
    USER_KEY,
    FakeExporter,
)

QUESTION = "What is the waiting period for illness claims?"


def good_values(**changes):
    values = {
        "LANGFUSE_PUBLIC_KEY": PUBLIC,
        "LANGFUSE_SECRET_KEY": SECRET,
        "LANGFUSE_HOST": HOST,
        "LANGFUSE_USER_HASH_KEY": USER_KEY,
        "JWT_SECRET_KEY": JWT_KEY,
        "AUDIT_HASH_KEY": AUDIT_KEY,
    }
    values.update(changes)
    return values


def run_smoke(tmp_path, values, make_sink=None, argv=None):
    path = tmp_path / "values.txt"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), "utf-8")
    holder = {}

    def setup(environ_, workdir, with_cache, role):
        (tmp_path / "rig").mkdir()
        rig = PipelineRig(tmp_path / "rig")
        holder["rig"] = rig
        return rig.deps, rig.db, rig.new_user(role)

    code = pipeline_smoke.main(
        argv or ["--run", "--load-env", "--langfuse", "--question", QUESTION],
        env_path=path,
        environ={},
        make_setup=setup,
        make_sink=make_sink,
    )
    return code, holder["rig"]


def test_it_sends_the_trace_and_prints_only_sent(tmp_path, capsys):
    exporter = FakeExporter()

    def make_sink(environ):
        assert langfuse_sink.config_from_env(environ) is not None  # the real configuration rules
        sink = LangfuseSink(exporter, USER_KEY)
        sink.start()
        return sink

    values = good_values()
    code, rig = run_smoke(tmp_path, values, make_sink)
    rig.close()
    out = capsys.readouterr().out
    assert code == 0 and "langfuse" in out and "SENT" in out
    for value in values.values():
        assert value not in out  # no key, no host, no value
    assert "waiting period" not in out.lower() and "30 days" not in out
    assert exporter.events and exporter.events[0]["type"] == "trace-create"


def test_a_failure_prints_only_the_exception_type(tmp_path, capsys):
    def make_sink(environ):
        sink = LangfuseSink(FakeExporter(error=ConnectionError("CANARYHOST0 refused")), USER_KEY)
        sink.start()
        return sink

    code, rig = run_smoke(tmp_path, good_values(), make_sink)
    rig.close()
    out = capsys.readouterr().out
    assert code == 1 and "FAILED" in out and "error=ConnectionError" in out
    assert "CANARYHOST0" not in out and "refused" not in out


def test_with_no_configuration_it_says_off_and_sends_nothing(tmp_path, capsys):
    code, rig = run_smoke(tmp_path, {})
    rig.close()
    out = capsys.readouterr().out
    assert code == 1 and "OFF" in out and "SENT" not in out


def test_a_refused_configuration_prints_the_exception_type_only(tmp_path, capsys):
    values = good_values()
    del values["LANGFUSE_USER_HASH_KEY"]  # refused: the user hash key is missing
    code, rig = run_smoke(tmp_path, values)
    rig.close()
    out = capsys.readouterr().out
    assert code == 1 and "SinkConfigError" in out
    for value in values.values():
        assert value not in out


def test_without_the_flag_no_sink_is_ever_built(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the sink must not be built without --langfuse")

    monkeypatch.setattr(langfuse_sink, "sink_from_env", forbidden)
    code, rig = run_smoke(tmp_path, good_values(), argv=["--run"])
    assert code == 0 and rig.deps.langfuse is None
    rig.close()


@pytest.mark.parametrize("name", langfuse_sink.ENV_NAMES)
def test_the_four_names_are_in_the_env_example_empty(name):
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text("utf-8")
    assert f"\n{name}=\n" in text
