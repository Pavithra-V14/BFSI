"""The dev-only live stage printer (spec 14, DEV_STAGE_TRACE)."""

import io
import re
from types import SimpleNamespace

import pytest

from app.observability.stage_printer import StagePrinter, printer_from_env
from tests.api_chat_fixtures import QUESTION, ChatEnv


def span(stage, ms, **fields):
    base = {
        "stage": stage,
        "duration_ms": ms,
        "status": "ok",
        "decider": None,
        "provider": None,
        "model": None,
        "key_fingerprint": None,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    base.update(fields)
    return SimpleNamespace(**base)


# ======================================================================= off unless asked ====
@pytest.mark.parametrize("value", [None, "", "false", "0", "yes", "1", "TRUEISH"])
def test_it_is_off_unless_the_flag_is_exactly_true(value):
    env = {} if value is None else {"DEV_STAGE_TRACE": value}
    assert printer_from_env(env, running_on_render=False) is None


def test_the_flag_true_switches_it_on_off_the_host():
    assert printer_from_env({"DEV_STAGE_TRACE": "true"}, running_on_render=False) is not None
    assert printer_from_env({"DEV_STAGE_TRACE": " TRUE "}, running_on_render=False) is not None


def test_it_is_refused_on_render_whatever_the_flag_says():
    assert printer_from_env({"DEV_STAGE_TRACE": "true"}, running_on_render=True) is None


# ============================================================================ what it prints ====
def test_each_stage_is_printed_as_it_starts_and_as_it_finishes_with_its_numbers():
    out = io.StringIO()
    printer = StagePrinter(out)
    printer.stage_started("triage")
    printer.stage_finished(
        span(
            "triage",
            42.5,
            decider="jev",
            provider="openrouter",
            model="m-1",
            key_fingerprint="ab12cd",
            input_tokens=120,
            output_tokens=8,
        )  # fmt: skip
    )
    text = out.getvalue()
    assert "> triage" in text and "< triage" in text and "42.5 ms" in text
    assert "jev" in text and "openrouter" in text and "m-1" in text and "ab12cd" in text
    assert "tokens in=120 out=8" in text


def test_a_stage_that_failed_says_how_it_ended():
    out = io.StringIO()
    StagePrinter(out).stage_finished(span("rerank", 5.0, status="timeout"))
    assert "[timeout]" in out.getvalue()


def test_the_waterfall_shows_shares_the_slowest_stage_and_unaccounted_time():
    out = io.StringIO()
    spans = [span("local_scan", 10.0), span("retrieve", 60.0), span("answer", 30.0)]
    StagePrinter(out).waterfall(spans, total_ms=125.0)
    text = out.getvalue()
    assert "slowest stage: retrieve (60.0 ms)" in text
    assert "unaccounted: 25.0 ms" in text and "total: 125.0 ms" in text
    assert re.search(r"retrieve\s+60\.0 ms\s+48\.0%", text)
    assert "slow" not in text.replace("slowest stage", "")  # no "slow" marking of a stage


def test_an_empty_request_prints_no_waterfall():
    out = io.StringIO()
    StagePrinter(out).waterfall([], 10.0)
    assert out.getvalue() == ""


# ===================================================================== never any query text ====
def test_a_whole_request_prints_stage_names_and_numbers_and_never_the_question(
    tmp_path, fast_hashing
):
    out = io.StringIO()
    printer = StagePrinter(out)
    env = ChatEnv(tmp_path)
    try:
        deps = env.rig.deps
        deps.on_stage_start = printer.stage_started
        deps.on_span = printer.stage_finished
        deps.on_finish = lambda ctx, result: printer.waterfall(
            list(ctx.trace.spans), (ctx.deps.clock() - ctx.started) * 1000.0
        )
        _, token = env.user_token("asha_rao")
        question = QUESTION + " My PAN is ABCDE1234F and my email is a.b@example.org"
        assert env.chat(token, question).status_code == 200
    finally:
        env.close()
    text = out.getvalue()
    assert "waterfall" in text and "slowest stage" in text and "unaccounted" in text
    for leaked in ("waiting period", "ABCDE1234F", "a.b@example.org", "30 days", "illness"):
        assert leaked not in text
    started = re.findall(r"^\s*> (\w+)", text, re.MULTILINE)
    finished = re.findall(r"^\s*< (\w+)", text, re.MULTILINE)
    assert started and sorted(started) == sorted(finished)


def test_the_hooks_do_nothing_when_the_printer_is_not_wired(tmp_path, fast_hashing, capsys):
    env = ChatEnv(tmp_path)
    try:
        _, token = env.user_token("asha_rao")
        env.chat(token)
    finally:
        env.close()
    assert "waterfall" not in capsys.readouterr().err
