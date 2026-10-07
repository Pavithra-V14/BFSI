"""Prompts as versioned files (spec 9.6).

Every prompt is a file `prompts/<name>.v<N>.md` with a fixed structure: a small header, then these
sections in this order, none empty:

    Role, Task, Constraints, Output format, Good example, Bad example, Refusal rule

The header (between two `---` lines) names the prompt, its version and what is sent to the model:
`sent: all` sends every section (with a short label each); `sent: task` sends the Task section
alone, word for word (used for the two one-line ingestion prompts, whose text must never drift: the
other sections are documentation). A `{name}` in the sent text is filled by `render(name=value)`.

The hash of a prompt (SHA-256 of the file with line endings normalised) is what every trace records
(spec 14), and `prompt_set_version` is the short hash of all active prompts, which goes into the
exact cache key (spec 10.1). Prompts stay short; `token_length` tracks their size.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.guardrails.context import neutralise

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
SECTIONS = (
    "Role",
    "Task",
    "Constraints",
    "Output format",
    "Good example",
    "Bad example",
    "Refusal rule",
)
_LABELS = {
    "Role": "ROLE",
    "Task": "TASK",
    "Constraints": "RULES",
    "Output format": "OUTPUT FORMAT",
    "Good example": "A GOOD ANSWER LOOKS LIKE THIS",
    "Bad example": "A BAD ANSWER (never do this)",
    "Refusal rule": "IF YOU CANNOT ANSWER",
}
_FILE_NAME = re.compile(r"^([a-z][a-z0-9_]*)\.v(\d+)\.md$")


class PromptError(Exception):
    """A prompt file breaks the fixed structure. The message names the file and the rule."""


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    sent: str  # "all" | "task"
    sections: Mapping[str, str] = field(repr=False)
    hash: str = ""

    @property
    def short_hash(self) -> str:
        return self.hash[:12]

    def text(self) -> str:
        """The text sent to the model, before any `{name}` is filled."""
        if self.sent == "task":
            return self.sections["Task"]
        return "\n\n".join(f"{_LABELS[name]}\n{self.sections[name]}" for name in SECTIONS)

    def render(self, **values: str) -> str:
        text = self.text()
        for key, value in values.items():
            text = text.replace("{" + key + "}", value)
        return text

    def token_length(self, counter: Any) -> int:
        return int(counter.count_text(self.text()))


def parse_prompt(raw: bytes | str, file_name: str) -> Prompt:
    data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    normalised = data.replace(b"\r\n", b"\n")
    text = normalised.decode("utf-8")
    named = _FILE_NAME.match(file_name)
    if not named:
        raise PromptError(f"{file_name}: the file name must look like name.v1.md")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---" or "---" not in [x.strip() for x in lines[1:]]:
        raise PromptError(f"{file_name}: it must start with a header between two --- lines")
    end = [x.strip() for x in lines[1:]].index("---") + 1
    header: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise PromptError(f"{file_name}: a header line must look like key: value")
        header[key.strip()] = value.split("#", 1)[0].strip()
    if header.get("name") != named.group(1) or header.get("version") != named.group(2):
        raise PromptError(f"{file_name}: the header name and version must match the file name")
    if header.get("sent") not in ("all", "task"):
        raise PromptError(f"{file_name}: the header needs sent: all or sent: task")
    sections: dict[str, list[str]] = {}
    order: list[str] = []
    current: str | None = None
    for line in lines[end + 1 :]:
        if line.startswith("## "):
            current = line[3:].strip()
            order.append(current)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
        elif line.strip():
            raise PromptError(f"{file_name}: text before the first section")
    if tuple(order) != SECTIONS:
        raise PromptError(
            f"{file_name}: the sections must be exactly, in order: {', '.join(SECTIONS)}"
        )
    bodies = {name: "\n".join(lines_).strip("\n") for name, lines_ in sections.items()}
    for name, body in bodies.items():
        if not body.strip():
            raise PromptError(f"{file_name}: the section '{name}' is empty")
    digest = hashlib.sha256(normalised).hexdigest()
    return Prompt(named.group(1), int(named.group(2)), header["sent"], bodies, digest)


def load_prompts(directory: Path = PROMPTS_DIR) -> dict[str, Prompt]:
    """The latest version of every prompt in the folder (a bad file stops the load)."""
    latest: dict[str, Prompt] = {}
    for path in sorted(directory.glob("*.md")):
        prompt = parse_prompt(path.read_bytes(), path.name)
        if prompt.name not in latest or prompt.version > latest[prompt.name].version:
            latest[prompt.name] = prompt
    return latest


@lru_cache(maxsize=1)
def _default() -> dict[str, Prompt]:
    return load_prompts()


def get_prompt(name: str, directory: Path | None = None) -> Prompt:
    prompts = load_prompts(directory) if directory is not None else _default()
    if name not in prompts:
        raise PromptError(f"there is no prompt named '{name}'")
    return prompts[name]


def prompt_set_version(prompts: Mapping[str, Prompt] | None = None) -> str:
    """A short fingerprint of every active prompt: the "prompt version" of the exact cache key. It
    changes when any prompt's text changes."""
    items = prompts if prompts is not None else _default()
    lines = sorted(f"{p.name}:v{p.version}:{p.hash}" for p in items.values())
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:12]


def describe_prompts(counter: Any, prompts: Mapping[str, Prompt] | None = None) -> list[dict]:
    """Name, version, hash and token length of each prompt: what the dashboard tracks over time."""
    items = prompts if prompts is not None else _default()
    return [
        {
            "name": p.name,
            "version": p.version,
            "hash": p.short_hash,
            "tokens": p.token_length(counter),
        }
        for p in sorted(items.values(), key=lambda p: p.name)
    ]


def assemble_answer(prompt: Prompt, context_block: str, question: str) -> str:
    """The generation prompt (spec 4.1 step 13): static instructions first, then the labelled
    chunks, then the question. No history, ever. The question is neutralised like a chunk: it
    cannot open or close a `<<<DOCUMENT ...>>>` block."""
    return f"{prompt.render()}\n\nDOCUMENTS\n{context_block}\n\nQUESTION\n{neutralise(question)}"


def assemble_rewrite(prompt: Prompt, summary: str, recent: str, message: str) -> str:
    """The rewrite prompt: the static instructions, then the running summary and the recent turns,
    then the latest message (all masked text). Like a chunk, each is neutralised (angle brackets
    replaced), so history or a question cannot forge a delimiter or a chat-template marker."""
    summary, recent, message = neutralise(summary), neutralise(recent), neutralise(message)
    return (
        f"{prompt.render()}\n\nSUMMARY OF THE CONVERSATION SO FAR\n{summary or '(none)'}"
        f"\n\nRECENT TURNS\n{recent or '(none)'}\n\nLATEST MESSAGE\n{message}"
    )
