"""Model output as plain text (spec 13.2 "Improper output handling", CLAUDE.md invariant).

Whatever a model writes is shown as plain text. Before it is stored or shown this module:
- removes control characters;
- strips HTML (tags, script and style blocks, comments, and tags hidden behind HTML entities);
- turns markdown links and images into their text: `[text](url)` becomes `text`,
  `![alt](url)` becomes `alt`, link reference definitions go;
- keeps a web address as inert plain text ONLY when its domain is a curated one (a regulator's or
  the company's, `app.guardrails.domains`); every other web address is removed;
- removes `javascript:`, `data:` and similar addresses.
It never renders anything: the caller prints the result as text.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable

from app.guardrails.domains import domain_is_seeded

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")
_BLOCKS = re.compile(r"<\s*(script|style)\b.*?<\s*/\s*\1\s*>|<!--.*?-->", re.IGNORECASE | re.DOTALL)
_AUTOLINK = re.compile(r"<((?:https?|ftp)://[^>\s]+)>", re.IGNORECASE)
_TAG = re.compile(r"</?[A-Za-z!?][^>]*>")
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]*)[^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)[^)]*\)")
_MD_REFERENCE = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_MD_DEFINITION = re.compile(r"^[ \t]*\[[^\]]+\]:[ \t]*\S+.*$", re.MULTILINE)
_URL = re.compile(r"(?:(?:https?|ftp)://|www\.)[^\s<>\"'`]+", re.IGNORECASE)
_SCHEME = re.compile(r"\b(?:javascript|vbscript|data|file):[^\s]*", re.IGNORECASE)
_TRAILING = ".,;:!?)]}"


def _strip_html(text: str) -> str:
    for _ in range(3):  # entities may hide a tag ("&lt;script&gt;"): decode, strip, repeat
        before = text
        text = html.unescape(text)
        text = _BLOCKS.sub("", text)
        text = _AUTOLINK.sub(r"\1", text)
        text = _TAG.sub("", text)
        if text == before:
            break
    return text


def to_plain_text(text: str, curated_domains: Iterable[str] = ()) -> str:
    domains = tuple(curated_domains)

    def keep(url: str) -> bool:
        return domain_is_seeded(url, domains)

    text = _CONTROL.sub("", text)
    text = _strip_html(text)
    text = _MD_IMAGE.sub(lambda m: m.group(1), text)

    def link(match: re.Match[str]) -> str:
        label, url = match.group(1).strip(), match.group(2)
        if label and keep(url):
            return f"{label} ({url})"
        return label

    text = _MD_LINK.sub(link, text)
    text = _MD_REFERENCE.sub(lambda m: m.group(1), text)
    text = _MD_DEFINITION.sub("", text)
    text = _SCHEME.sub("", text)

    def bare(match: re.Match[str]) -> str:
        url = match.group(0)
        stripped = url.rstrip(_TRAILING)
        tail = url[len(stripped) :]
        return (stripped + tail) if keep(stripped) else tail

    text = _URL.sub(bare, text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
