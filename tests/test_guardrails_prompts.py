"""Prompts as versioned files (spec 9.6): structure, hash, version, token length, and the two
ingestion prompts that moved out of code and must not change by a single byte."""

import hashlib
from pathlib import Path

import pytest

from app.guardrails import prompts
from app.guardrails.prompts import (
    PROMPTS_DIR,
    SECTIONS,
    PromptError,
    assemble_answer,
    assemble_rewrite,
    describe_prompts,
    get_prompt,
    load_prompts,
    parse_prompt,
    prompt_set_version,
)
from tests.helpers import WordCounter

# The text the code sent before it moved into prompts/ (Phase 2 and Phase 5). The saved-answer cache
# and the paid calls are keyed on what was sent, so it must stay byte for byte the same.
OLD_CAPTION = (
    "Describe this image from a financial document so that it can be found by search. "
    "Say what kind of image it is, any title, labels and axes, and the figures, trends or "
    "relationships it shows. Plain text, under 150 words. Only describe what is visible."
)
OLD_TABLE_SUMMARY_PREFIX = (
    "Write ONE plain sentence of at most 30 words saying what this table shows. "
    "No preamble, no quotes, no bullet.\n\n"
)

GOOD = """---
name: demo
version: 1
sent: all
---
## Role
You help.

## Task
Do the thing.

## Constraints
- Be brief.

## Output format
Plain text.

## Good example
Yes.

## Bad example
No. This is wrong because it is long.

## Refusal rule
Say you cannot.
"""


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


# ---- the files that exist ----------------------------------------------------------------------
def test_the_expected_prompts_exist_and_follow_the_structure():
    found = load_prompts()
    assert {"answer", "rewrite", "summary", "caption", "table_summary"} <= set(found)
    for prompt in found.values():
        assert tuple(prompt.sections) == SECTIONS
        assert all(body.strip() for body in prompt.sections.values())
        assert prompt.version >= 1 and len(prompt.hash) == 64


def test_every_file_in_the_folder_is_a_valid_prompt():
    for path in PROMPTS_DIR.glob("*.md"):
        parse_prompt(path.read_bytes(), path.name)


def test_the_two_ingestion_prompts_are_byte_identical_to_the_old_text():
    caption = get_prompt("caption").render()
    assert caption == OLD_CAPTION and sha(caption) == sha(OLD_CAPTION)
    table = "Plan | Limit\nGold | 5 lakh"
    rendered = get_prompt("table_summary").render(table=table)
    assert rendered == OLD_TABLE_SUMMARY_PREFIX + table
    assert sha(rendered) == sha(OLD_TABLE_SUMMARY_PREFIX + table)
    assert get_prompt("table_summary").text().endswith("{table}")


def test_the_ingestion_code_sends_exactly_the_file_text():
    import asyncio

    from app.ingest.llm import LlmTools
    from app.ingest.runner import LoopRunner

    sent = {}

    class Gateway:
        async def caption_image(self, image, mime, *, prompt, max_wait_s):
            sent["caption"] = prompt

            class R:
                text = "x"

            return R()

        async def complete(self, messages, **kwargs):
            sent["summary"] = messages[0]["content"]

            class R:
                text = "x"

            return R()

    runner = LoopRunner()
    try:
        tools = LlmTools(Gateway(), runner)
        tools.caption(b"\x89PNG\r\n\x1a\n" + b"x" * 8)
        tools.summarise("Plan | Limit")
    finally:
        runner.close()
    assert sent["caption"] == OLD_CAPTION
    assert sent["summary"] == OLD_TABLE_SUMMARY_PREFIX + "Plan | Limit"
    assert asyncio  # the runner drives the coroutines


def test_the_gateway_no_longer_owns_the_caption_text():
    source = (Path(prompts.__file__).parents[1] / "gateway" / "gateway.py").read_text("utf-8")
    assert "Describe this image" not in source


# ---- hash and version --------------------------------------------------------------------------
def test_the_hash_is_the_sha256_of_the_file_with_normalised_line_endings():
    raw = (PROMPTS_DIR / "answer.v1.md").read_bytes()
    assert get_prompt("answer").hash == hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def test_windows_line_endings_do_not_change_the_hash_or_the_text():
    lf = parse_prompt(GOOD, "demo.v1.md")
    crlf = parse_prompt(GOOD.replace("\n", "\r\n").encode(), "demo.v1.md")
    assert (lf.hash, lf.sections) == (crlf.hash, crlf.sections)


def test_any_change_to_the_file_changes_the_hash_and_the_set_version():
    one = parse_prompt(GOOD, "demo.v1.md")
    two = parse_prompt(GOOD.replace("Be brief.", "Be brief!"), "demo.v1.md")
    assert one.hash != two.hash and one.short_hash == one.hash[:12]
    assert prompt_set_version({"demo": one}) != prompt_set_version({"demo": two})
    assert prompt_set_version({"demo": one}) == prompt_set_version({"demo": one})


def test_the_set_version_covers_every_prompt_and_is_short():
    version = prompt_set_version()
    assert len(version) == 12 and version == prompt_set_version(load_prompts())
    smaller = {k: v for k, v in load_prompts().items() if k != "summary"}
    assert prompt_set_version(smaller) != version


def test_the_latest_version_of_a_prompt_is_the_one_loaded(tmp_path):
    (tmp_path / "demo.v1.md").write_text(GOOD)
    (tmp_path / "demo.v2.md").write_text(
        GOOD.replace("version: 1", "version: 2").replace("Do the thing.", "Do the new thing.")
    )
    assert get_prompt("demo", tmp_path).version == 2
    assert "new thing" in get_prompt("demo", tmp_path).sections["Task"]


# ---- the structure is enforced -----------------------------------------------------------------
@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda t: t.replace("## Bad example\nNo. This is wrong because it is long.\n\n", ""),
            "sections must be",
        ),
        (lambda t: t.replace("## Role", "## Persona"), "sections must be"),
        (
            lambda t: t.replace(
                "## Good example\nYes.\n\n## Bad example",
                "## Bad example\nNo.\n\n## Good example\nYes.\n\n## Bad2",
            ),
            "sections must be",
        ),
        (lambda t: t + "\n## Extra\nmore\n", "sections must be"),
        (lambda t: t.replace("Do the thing.", ""), "'Task' is empty"),
        (lambda t: t.replace("---\nname: demo", "name: demo"), "header"),
        (lambda t: t.replace("name: demo", "name: other"), "match the file name"),
        (lambda t: t.replace("version: 1", "version: 2"), "match the file name"),
        (lambda t: t.replace("sent: all", "sent: some"), "sent: all or sent: task"),
        (lambda t: t.replace("sent: all\n", ""), "sent: all or sent: task"),
        (
            lambda t: t.replace("---\n## Role", "---\nstray words\n## Role"),
            "before the first section",
        ),
    ],
)
def test_a_file_that_breaks_the_structure_is_refused(change, message):
    with pytest.raises(PromptError, match=message):
        parse_prompt(change(GOOD), "demo.v1.md")


@pytest.mark.parametrize(
    "name", ["demo.md", "Demo.v1.md", "demo.v1.txt", "demo.vx.md", "1demo.v1.md"]
)
def test_the_file_name_must_carry_name_and_version(name):
    with pytest.raises(PromptError, match="file name"):
        parse_prompt(GOOD, name)


def test_a_bad_file_stops_the_whole_load(tmp_path):
    (tmp_path / "demo.v1.md").write_text(GOOD)
    (tmp_path / "broken.v1.md").write_text("no header")
    with pytest.raises(PromptError):
        load_prompts(tmp_path)


def test_an_unknown_prompt_is_an_error():
    with pytest.raises(PromptError, match="no prompt named"):
        get_prompt("nope")


# ---- what is sent ------------------------------------------------------------------------------
def test_sent_all_sends_every_section_in_order_with_a_label():
    text = parse_prompt(GOOD, "demo.v1.md").text()
    order = [
        "ROLE",
        "TASK",
        "RULES",
        "OUTPUT FORMAT",
        "A GOOD ANSWER",
        "A BAD ANSWER",
        "IF YOU CANNOT",
    ]
    positions = [text.index(label) for label in order]
    assert positions == sorted(positions)
    assert "Be brief." in text and "Say you cannot." in text


def test_sent_task_sends_only_the_task_section():
    prompt = parse_prompt(GOOD.replace("sent: all", "sent: task"), "demo.v1.md")
    assert prompt.text() == "Do the thing."


def test_render_fills_only_the_names_it_is_given():
    prompt = parse_prompt(GOOD.replace("Do the thing.", "Do {x} with {y}."), "demo.v1.md")
    assert "Do 1 with {y}." in prompt.render(x="1")
    assert "Do {braces} with {y}." in prompt.render(x="{braces}")  # values are never re-expanded


def test_the_answer_prompt_has_the_static_part_first_then_chunks_then_the_question():
    block = "<<<DOCUMENT id=S1 source=a effective=b clause=c>>>\ntext\n<<<END DOCUMENT>>>"
    text = assemble_answer(get_prompt("answer"), block, "What is KYC?")
    assert (
        text.index("ROLE")
        < text.index("DOCUMENTS\n")
        < text.index(block)
        < text.index("QUESTION\n")
    )
    assert text.endswith("What is KYC?")
    import inspect

    assert (
        "history" not in inspect.signature(assemble_answer).parameters
    )  # the generator never sees it


def test_the_rewrite_prompt_carries_the_static_bfsi_checklist_and_the_inputs():
    prompt = get_prompt("rewrite")
    for word in ("product", "waiting period", "charges", "documents", "claim process", "grievance"):
        assert word in prompt.sections["Constraints"]
    text = assemble_rewrite(prompt, "SUMMARY-X", "TURNS-Y", "MESSAGE-Z")
    assert text.index("SUMMARY-X") < text.index("TURNS-Y") < text.index("MESSAGE-Z")
    assert "(none)" in assemble_rewrite(prompt, "", "", "hello there friend")


def test_the_answer_prompt_has_no_secrets_and_no_links_outside_the_bad_example():
    prompt = get_prompt("answer")
    for word in ("api_key", "token", "password", "secret"):
        assert word not in prompt.text().lower()
    for name, body in prompt.sections.items():
        if name != "Bad example":  # the bad example names a link on purpose
            assert "http://" not in body.lower() and "https://" not in body.lower(), name
            assert "www." not in body.lower(), name
    assert "http" in prompt.sections["Bad example"]  # it really does, so this test can fail


# ---- length is tracked and kept short --------------------------------------------------------
def test_each_prompt_has_a_token_length_and_stays_short():
    counter = WordCounter()  # words stand in for tokens
    rows = {row["name"]: row for row in describe_prompts(counter)}
    assert set(rows) >= {"answer", "rewrite", "summary", "caption", "table_summary"}
    for name, row in rows.items():
        assert row["tokens"] > 0 and len(row["hash"]) == 12 and row["version"] >= 1
        assert row["tokens"] <= 450, (name, row["tokens"])
    assert rows["caption"]["tokens"] < 60 and rows["table_summary"]["tokens"] < 60
