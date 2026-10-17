"""Cassette hygiene (spec 16: "keys scrubbed"). A cassette is a recording of a real provider answer
and is committed, so it must hold no key, no token and no real vector.

Three layers are tested: the scrubbing functions (what is removed while recording), the detector
(what refuses a file afterwards), and the committed files themselves (every cassette in
tests/cassettes passes the detector, however it got there)."""

import json
import secrets

import pytest
import vcr.request

from scripts import cassette_tools as tools
from scripts import record_cassettes


def made_up_key(prefix: str = "") -> str:
    """A key-shaped string made at test time, so no key-shaped literal sits in this file."""
    return prefix + secrets.token_urlsafe(30)


# ================================================================================== scrubbing
def request(uri="https://api.example.test/v1/rerank", headers=None):
    return vcr.request.Request("POST", uri, b"{}", headers or {})


def test_the_authorization_header_and_other_key_headers_are_replaced():
    token = made_up_key()
    scrubbed = tools.scrub_request(
        request(
            headers={
                "Authorization": "Bearer " + token,
                "x-goog-api-key": token,
                "Content-Type": "application/json",
            }
        )
    )
    assert scrubbed.headers["Authorization"] == tools.REDACTED
    assert scrubbed.headers["x-goog-api-key"] == tools.REDACTED
    assert scrubbed.headers["Content-Type"] == "application/json"
    assert token not in json.dumps(dict(scrubbed.headers))


def test_a_key_in_the_query_string_is_replaced_and_other_parameters_stay():
    token = made_up_key()
    scrubbed = tools.scrub_request(request(uri=f"https://h.test/p?key={token}&alt=json"))
    assert token not in scrubbed.uri
    assert "key=REDACTED" in scrubbed.uri and "alt=json" in scrubbed.uri


def response(body, headers=None):
    return {
        "status": {"code": 200, "message": "OK"},
        "headers": headers or {},
        "body": {"string": json.dumps(body)},
    }


def test_response_headers_are_dropped_except_the_content_type():
    scrubbed = tools.scrub_response(
        response(
            {"ok": 1},
            {"Content-Type": ["application/json"], "Set-Cookie": ["a=b"], "X-Request-Id": ["abc"]},
        )  # fmt: skip
    )
    assert set(scrubbed["headers"]) == {"Content-Type"}


def test_an_embedding_vector_is_cut_and_comes_back_at_its_real_length():
    vector = [round(0.001 * (i + 1), 6) for i in range(768)]
    scrubbed = tools.scrub_response(response({"embeddings": [{"values": vector}]}))
    stored = json.loads(scrubbed["body"]["string"])
    assert len(stored["embeddings"][0]["values"]) == tools.KEEP_VALUES
    assert stored["embeddings"][0]["values" + tools.LENGTH_SUFFIX] == 768
    # the file holds the short vector; loading it restores the length (zeros after the kept ones)
    cassette = {"interactions": [{"response": {"body": {"string": scrubbed["body"]["string"]}}}]}
    loaded = tools.PaddedJson.deserialize(json.dumps(cassette))
    restored = json.loads(loaded["interactions"][0]["response"]["body"]["string"])
    values = restored["embeddings"][0]["values"]
    assert len(values) == 768 and set(values[8:]) == {0.0}
    assert not any(k.endswith(tools.LENGTH_SUFFIX) for k in restored["embeddings"][0])


def test_a_short_list_of_numbers_is_left_alone():
    scrubbed = tools.scrub_response(response({"probabilities": [0.1, 0.9]}))
    assert json.loads(scrubbed["body"]["string"]) == {"probabilities": [0.1, 0.9]}


def test_a_long_high_entropy_token_in_a_response_is_replaced_but_words_are_not():
    identifier = made_up_key("gen-")
    scrubbed = tools.scrub_response(
        response({"id": identifier, "text": "A waiting period of thirty days applies."})
    )
    stored = json.loads(scrubbed["body"]["string"])
    assert stored["id"] == tools.REDACTED + "-TOKEN"
    assert stored["text"] == "A waiting period of thirty days applies."


# =================================================================================== detector
def test_the_detector_passes_a_clean_cassette_text():
    text = json.dumps(
        {
            "request": {
                "headers": {"Authorization": "REDACTED"},
                "uri": "https://h.test/p?key=REDACTED",
            }
        }
    )
    assert tools.hygiene_problems(text) == []


@pytest.mark.parametrize(
    "make",
    [
        lambda: '{"authorization": "Bearer ' + made_up_key() + '"}',
        lambda: '{"uri": "https://h.test/p?key=' + made_up_key() + '"}',
        lambda: '{"x-goog-api-key": "' + made_up_key() + '"}',
        lambda: '{"note": "' + made_up_key("sk-") + '"}',
        lambda: '{"note": "' + made_up_key("AIza") + '"}',
        lambda: '{"note": "' + secrets.token_hex(24) + '"}',
    ],
)
def test_the_detector_refuses_a_key_a_credential_header_or_a_random_long_string(make):
    assert tools.hygiene_problems(make())


def test_a_problem_report_never_holds_the_offending_text():
    token = made_up_key("sk-")
    shown = " ".join(tools.hygiene_problems('{"n": "' + token + '"}'))
    assert token not in shown


def test_every_committed_cassette_passes_the_detector():
    for path in sorted(tools.CASSETTE_DIR.glob("*.json")):
        assert tools.hygiene_problems(path.read_text("utf-8")) == [], path.name


def test_only_the_five_known_cassettes_may_be_committed():
    names = (
        {p.stem for p in tools.CASSETTE_DIR.glob("*.json")}
        if tools.CASSETTE_DIR.is_dir()
        else set()
    )
    assert names <= set(tools.NAMES)


def test_a_committed_cassette_holds_no_full_length_vector():
    for path in sorted(tools.CASSETTE_DIR.glob("*.json")):
        data = json.loads(path.read_text("utf-8"))
        for interaction in data["interactions"]:
            body = json.loads(interaction["response"]["body"]["string"] or "{}")
            assert _longest_number_list(body) <= tools.KEEP_VALUES, path.name


def _longest_number_list(value) -> int:
    if isinstance(value, list):
        numbers = all(isinstance(x, (int, float)) for x in value)
        return max([len(value) if numbers else 0, *(_longest_number_list(v) for v in value)])
    if isinstance(value, dict):
        return max([0, *(_longest_number_list(v) for v in value.values())])
    return 0


# ================================================================================== recorder
def keys(**overrides):
    values = {
        "COHERE_API_KEY": made_up_key(),
        "JINA_API_KEY": made_up_key(),
        "JEV_OPENROUTER_API_KEY": made_up_key(),
        "GEMINI_EMBEDDING_KEY_A": made_up_key(),
        "OPENROUTER_API_KEYS": made_up_key() + "," + made_up_key(),
    }
    values.update(overrides)
    return values


class Lines(list):
    def __call__(self, text):
        self.append(text)


def test_without_run_nothing_is_called(tmp_path):
    called = []

    async def scenario(k):
        called.append(1)

    out = Lines()
    status = record_cassettes.main(
        [], keys(), out=out, folder=tmp_path, scenarios={n: scenario for n in tools.NAMES}
    )
    assert status == 0 and called == [] and "nothing was called" in "\n".join(out)
    assert not list(tmp_path.iterdir())


def test_a_missing_key_stops_before_any_call_and_names_only_the_setting(tmp_path):
    called = []

    async def scenario(k):
        called.append(1)

    env = keys()
    del env["COHERE_API_KEY"]
    out = Lines()
    status = record_cassettes.main(
        ["--run"], env, out=out, folder=tmp_path, scenarios={n: scenario for n in tools.NAMES}
    )
    shown = "\n".join(out)
    assert status == 1 and called == [] and "COHERE_API_KEY" in shown
    assert not any(value in shown for value in env.values())


def test_on_render_nothing_is_recorded(tmp_path):
    out = Lines()
    status = record_cassettes.main(
        ["--run"], {**keys(), "RENDER": "true"}, out=out, folder=tmp_path, scenarios={}
    )
    assert status == 1 and "never on Render" in "\n".join(out)


def test_a_failing_call_writes_nothing_and_prints_the_class_only(tmp_path):
    async def failing(k):
        raise RuntimeError("provider said: " + k["cohere"])

    out = Lines()
    env = keys()
    status = record_cassettes.main(
        ["--run", "--only", "cohere_rerank"], env, out=out, folder=tmp_path,
        scenarios={"cohere_rerank": failing},
    )  # fmt: skip
    shown = "\n".join(out)
    assert status == 1 and "FAILED (RuntimeError)" in shown
    assert env["COHERE_API_KEY"] not in shown and not list(tmp_path.iterdir())


class ProviderError(Exception):
    status_code = 503


def test_explain_prints_a_scrubbed_detail_and_without_it_only_the_class(tmp_path):
    secret = made_up_key()
    other = "Auth" + "orization: " + "Bear" + "er " + "abcdefghijklmnopqrstuvwxyz0123456789"

    async def failing(k):
        raise ProviderError(
            f"POST https://example.test/v1/embed?key={secret} failed; {other}; "
            f"body {'x' * 40}; model gemini-embedding-2 not found"
        )

    env = keys(COHERE_API_KEY=secret)
    plain = Lines()
    record_cassettes.main(
        ["--run", "--only", "cohere_rerank"], env, out=plain, folder=tmp_path,
        scenarios={"cohere_rerank": failing},
    )  # fmt: skip
    assert "detail" not in "\n".join(plain)
    out = Lines()
    status = record_cassettes.main(
        ["--run", "--only", "cohere_rerank", "--explain"], env, out=out, folder=tmp_path,
        scenarios={"cohere_rerank": failing},
    )  # fmt: skip
    shown = "\n".join(out)
    assert status == 1 and "detail: ProviderError status=503" in shown
    assert secret not in shown and "abcdefghijklmnopqrstuvwxyz" not in shown
    assert "x" * 30 not in shown and "?key=" not in shown
    assert "gemini-embedding-2 not found" in shown, "the useful words stay"
    assert not list(tmp_path.iterdir())


def test_a_recording_that_breaks_the_hygiene_rules_is_deleted(tmp_path):
    async def leaky(k):
        tools.cassette_path("cohere_rerank").write_text(
            '{"note": "' + made_up_key("sk-") + '"}', "utf-8"
        )

    out = Lines()
    status = record_cassettes.main(
        ["--run", "--only", "cohere_rerank"], keys(), out=out, folder=tmp_path,
        scenarios={"cohere_rerank": leaky},
    )  # fmt: skip
    assert status == 1 and "DELETED" in "\n".join(out)
    assert not (tmp_path / "cohere_rerank.json").exists()


@pytest.mark.parametrize(
    "text",
    [
        '{"h": "Bearer abcdefgh12345678"}',
        '{"uri": "https://h.test/p?key=abcd1234"}',
        '{"note": "sk-aaaaaaaaaaaaaaaaaaaa"}',
        '{"note": "gsk_aaaaaaaaaaaaaaaaaaaaaaaa"}',
        '{"note": "-----BEGIN ' + 'PRIVATE KEY-----"}',  # made from two pieces on purpose
    ],
)
def test_the_detector_has_its_own_rule_for_each_key_shape_not_only_the_entropy_rule(text):
    """Short or repetitive values pass the entropy rule; the shape rules must still refuse them."""
    assert tools.hygiene_problems(text)


# ==== list-valued headers, real key values, request bodies ====
def test_a_credential_header_held_as_a_list_is_checked_like_a_string():
    leaked = {"interactions": [{"request": {"headers": {"Authorization": ["abc"]}}}]}
    clean = {"interactions": [{"request": {"headers": {"Authorization": [tools.REDACTED]}}}]}
    assert "a credential header that is not replaced" in tools.hygiene_problems(json.dumps(leaked))
    assert tools.hygiene_problems(json.dumps(clean)) == []
    string_leak = {"request": {"headers": {"x-goog-api-key": "abc"}}}
    assert tools.hygiene_problems(json.dumps(string_leak))


def test_a_list_valued_header_is_replaced_while_recording():
    token = made_up_key()
    scrubbed = tools.scrub_request(
        request(headers={"Authorization": ["Bearer " + token], "Accept": ["json"]})
    )
    assert tools.REDACTED in str(scrubbed.headers["Authorization"])
    assert token not in str(dict(scrubbed.headers))
    assert "json" in str(scrubbed.headers["Accept"])


def test_a_real_key_value_anywhere_in_the_file_is_refused_whatever_it_looks_like():
    plain = "abcabcabcabcabc1"  # short and repetitive: no shape rule and no entropy rule fires
    text = json.dumps({"note": "echo " + plain})
    assert tools.hygiene_problems(text) == []
    assert tools.hygiene_problems(text, forbidden=[plain])
    assert plain not in " ".join(tools.hygiene_problems(text, forbidden=[plain]))


def test_the_recorder_checks_the_files_for_the_real_key_values_it_used(tmp_path):
    plain = "abcabcabcabcabc1"

    async def echo(k):
        tools.cassette_path("cohere_rerank").write_text(json.dumps({"x": plain}), "utf-8")

    out = Lines()
    status = record_cassettes.main(
        ["--run", "--only", "cohere_rerank"],
        keys(COHERE_API_KEY=plain),
        out=out,
        folder=tmp_path,
        scenarios={"cohere_rerank": echo},
    )
    assert status == 1 and "DELETED" in "\n".join(out)
    assert plain not in "\n".join(out) and not (tmp_path / "cohere_rerank.json").exists()


def test_the_text_of_a_request_body_is_replaced_and_its_call_parameters_kept():
    body = json.dumps(
        {
            "model": "gemini-embedding-2",
            "dimensions": 768,
            "input": ["What is my PAN ABCPE1234F?", "second text"],
            "messages": [{"role": "user", "content": "private question about my policy"}],
        }
    ).encode()
    scrubbed = tools.scrub_request(vcr.request.Request("POST", "https://h.test/p", body, {}))
    data = json.loads(scrubbed.body)
    assert data["model"] == "gemini-embedding-2" and data["dimensions"] == 768
    assert data["input"] == [tools.BODY_PLACEHOLDER, tools.BODY_PLACEHOLDER]
    assert data["messages"][0] == {"role": "user", "content": tools.BODY_PLACEHOLDER}
    assert b"ABCPE1234F" not in scrubbed.body and b"private question" not in scrubbed.body


def test_a_request_body_that_is_not_json_is_dropped():
    scrubbed = tools.scrub_request(
        vcr.request.Request("POST", "https://h.test/p", b"user=me&text=private", {})
    )
    assert b"private" not in scrubbed.body
