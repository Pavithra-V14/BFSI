"""Structured JSON logs and the PII / secret scan (spec 14, Phase 12)."""

import io
import json
import logging
import sys

import pytest

from app.observability import logs, scan
from tests.api_chat_fixtures import QUESTION, ChatEnv

# Built from pieces: this file holds no text of the shape a secret scanner refuses.
PROVIDER_KEY = "sk-or-v1-" + "abcdefghij" + "klmnopqrstuv"
ASSIGNED = "api" + "_key" + ' = "' + "abcdefgh" + "12345678" + '"'
CREDENTIAL_URL = "postgres://" + "user:" + "hunter2hunter2" + "@db.example.test/x"


@pytest.fixture
def stream():
    """Route every log line through the JSON handler into a buffer, then put logging back."""
    root = logging.getLogger()
    before_handlers, before_level = list(root.handlers), root.level
    buffer = io.StringIO()
    logs.configure_logging("INFO", buffer)
    yield buffer
    for handler in list(root.handlers):
        if handler not in before_handlers:
            root.removeHandler(handler)
    root.setLevel(before_level)


def lines(buffer):
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


# ================================================================================ the format ====
def test_a_log_line_is_one_json_object_with_the_fixed_keys(stream):
    logging.getLogger("app.test").warning("something happened %s", 3)
    (line,) = lines(stream)
    assert set(line) >= {"ts", "level", "logger", "correlation_id", "message"}
    assert line["level"] == "WARNING" and line["message"] == "something happened 3"


def test_the_correlation_id_is_on_every_line_made_while_it_is_bound(stream):
    token = logs.bind_correlation("abc123")
    try:
        logging.getLogger("app.test").info("inside")
    finally:
        logs.reset_correlation(token)
    logging.getLogger("app.test").info("outside")
    inside, outside = lines(stream)
    assert inside["correlation_id"] == "abc123" and outside["correlation_id"] is None


def test_an_exception_is_logged_as_its_class_name_only(stream):
    try:
        raise ValueError("odd message with 4111111111111111")
    except ValueError:
        logging.getLogger("app.test").exception("failed")
    (line,) = lines(stream)
    assert line["exc_type"] == "ValueError"
    text = stream.getvalue()
    assert "4111111111111111" not in text and "Traceback" not in text


def test_only_allow_listed_fields_pass_and_only_as_plain_words_or_numbers(stream):
    logging.getLogger("app.test").info(
        "x",
        extra={
            "fx": {
                "stage": "retrieve",
                "count": 3,
                "duration_ms": 12.5,
                "body": "My email is a@b.example",
                "headers": {"authorization": "Bearer abcdefghijklmnop"},
                "query": "what is my claim status",
                "reason": "a whole sentence with spaces, which is not a word",
            }
        },
    )
    (line,) = lines(stream)
    assert line["fields"] == {"stage": "retrieve", "count": 3, "duration_ms": 12.5}


def test_a_plain_word_under_a_name_that_is_not_allow_listed_is_dropped(stream):
    logging.getLogger("app.test").info(
        "x", extra={"fx": {"username": "asha_rao", "token": "abc123", "stage": "retrieve"}}
    )
    (line,) = lines(stream)
    assert line["fields"] == {"stage": "retrieve"}  # the name decides, not only the value


def test_the_message_is_scrubbed_of_addresses_numbers_and_tokens(stream):
    logging.getLogger("app.test").warning(
        "mail a@b.example phone 9876543210 acct 123456789012 "
        "bearer abcdefghijklmnopqrstuvwx https://x.test/p?token=abc"
    )
    (line,) = lines(stream)
    for leaked in (
        "a@b.example",
        "9876543210",
        "123456789012",
        "abcdefghijklmnopqrstuvwx",
        "token=abc",
    ):
        assert leaked not in line["message"]
    assert scan.scan_text(stream.getvalue()) == []


def test_the_access_log_has_no_query_string_and_no_client_address(stream):
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("203.0.113.9:5000", "GET", "/sessions?token=abcdef&q=my+question", "1.1", 200),
        None,
    )
    out = json.loads(logs.JsonFormatter().format(record))
    assert out["fields"] == {"method": "GET", "path": "/sessions", "status_code": 200}
    assert "203.0.113.9" not in json.dumps(out) and "abcdef" not in json.dumps(out)


def test_configuring_twice_does_not_double_the_handler(stream):
    logs.configure_logging("INFO", stream)
    logging.getLogger("app.test").warning("once")
    assert len(lines(stream)) == 1


# ====================================== nothing of a request reaches the log, end to end ====
@pytest.fixture
def env(tmp_path, fast_hashing, stream):
    made = ChatEnv(tmp_path)
    yield made
    made.close()


HOSTILE_QUESTION = (
    "My name is Rahul Sharma, email rahul.sharma@example.org, phone 9876543210, "
    "PAN ABCDE1234F, account 123456789012. " + QUESTION
)


def test_a_whole_chat_leaves_no_body_header_query_or_pii_in_the_log(env, stream):
    _, token = env.user_token("asha_rao")
    response = env.client.post(
        "/chat?session_secret=querystringvalue&email=q@example.org",
        json={"message": HOSTILE_QUESTION},
        headers={**env.bearer(token), "X-Custom": "headervalue-9f8e7d6c"},
    )
    assert response.status_code == 200
    text = stream.getvalue()
    for leaked in (
        "rahul.sharma@example.org",
        "9876543210",
        "ABCDE1234F",
        "123456789012",
        "Rahul Sharma",
        "querystringvalue",
        "q@example.org",
        "headervalue-9f8e7d6c",
        token,
        QUESTION,
    ):
        assert leaked not in text, leaked
    assert scan.scan_text(text) == []


def test_a_failed_request_logs_no_text_either(env, stream):
    env.client.post("/chat", json={"message": HOSTILE_QUESTION}, headers={"Authorization": "x"})
    assert "Rahul" not in stream.getvalue() and "9876543210" not in stream.getvalue()


def test_the_correlation_id_is_internal_and_the_same_one_names_the_trace(env, monkeypatch, stream):
    seen = {}
    real = env.rig.pipeline.answer

    async def spy(request, db, **kwargs):
        seen["id"] = request.correlation_id
        logging.getLogger("app.test").info("inside the request")  # a line made by the request
        return await real(request, db, **kwargs)

    monkeypatch.setattr(env.rig.pipeline, "answer", spy)
    _, token = env.user_token("asha_rao")
    response = env.chat(token)
    assert response.status_code == 200
    assert len(seen["id"]) == 32  # one id per HTTP request, made by the service
    logged = [line for line in lines(stream) if line["message"] == "inside the request"]
    assert [line["correlation_id"] for line in logged] == [seen["id"]]  # the trace id is the log id
    assert seen["id"] not in response.text
    assert all(seen["id"] not in value for value in response.headers.values())


# ============================================================================== the scanner ====
@pytest.mark.parametrize(
    ("rule", "text"),
    [
        ("email", "contact someone@example.org now"),
        ("indian-mobile", "call 9876543210 today"),
        ("long-number", "account 123456789012 is closed"),
        ("pan", "PAN is ABCDE1234F here"),
        ("bearer", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"),
        ("jwt", "t=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdEFGH"),
        ("provider-key", "key " + PROVIDER_KEY),
        ("url-credentials", CREDENTIAL_URL),
        ("url-query", "GET https://api.example.test/x?token=abc"),
        ("secret-assignment", ASSIGNED),
    ],
)
def test_the_scanner_finds_each_kind_by_rule_name_and_line(rule, text):
    found = scan.scan_text("first line is fine\n" + text)
    assert [(f.rule, f.line) for f in found if f.rule == rule] == [(rule, 2)]


def test_clean_log_lines_have_no_findings():
    clean = '{"level": "INFO", "message": "api event cache_error", "fields": {"count": 3}}'
    assert scan.scan_text(clean) == []


def test_the_command_reports_rules_and_lines_and_never_the_matched_text(tmp_path, capsys):
    path = tmp_path / "log.txt"
    path.write_text("ok\nuser someone@example.org said hi\n", encoding="utf-8")
    code = scan.main([str(path)])
    out = capsys.readouterr().out
    assert code == 1 and "line 2: email" in out and "someone@example.org" not in out


def test_the_command_exits_zero_on_a_clean_file_and_two_on_a_missing_one(tmp_path, capsys):
    path = tmp_path / "log.txt"
    path.write_text("all good\n", encoding="utf-8")
    assert scan.main([str(path)]) == 0
    assert scan.main([str(tmp_path / "nope.txt")]) == 2


def test_the_scan_module_runs_as_a_command(tmp_path):
    import subprocess

    path = tmp_path / "log.txt"
    path.write_text("phone 9876543210\n", encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-m", "app.observability.scan", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 1 and "9876543210" not in done.stdout


# ============================== the test run itself: logging is put back after each test ====
def test_zz_a_test_may_switch_the_process_to_json_logs():
    logs.configure_logging("INFO", io.StringIO())
    assert any(getattr(h, "_bfsi_json", False) for h in logging.getLogger().handlers)


def test_zz_and_the_next_test_finds_logging_as_it_was():
    assert not any(getattr(h, "_bfsi_json", False) for h in logging.getLogger().handlers)
