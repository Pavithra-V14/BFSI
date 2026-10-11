"""The PII and secret scan for logs and trace payloads (spec 14: "a scan confirms traces and logs
contain no PII or secrets").

Two uses of the same patterns:
- `scrub(text)`: the log formatter calls it on every message; a match is replaced by a fixed word,
  so what is logged never holds one.
- `scan_text(text)` / the command `python -m app.observability.scan FILE`: reads a log file (or any
  text file, for example a saved trace payload) and reports WHICH rule matched on WHICH line. It
  never prints the matched text. Exit code 0: nothing found; 1: something found; 2: usage error.

The patterns are a net, not a proof: they find e-mail addresses, Indian mobile numbers, long digit
runs (account, Aadhaar and card numbers), PAN numbers, bearer tokens and JWTs, provider key
shapes, an address with credentials or a query string, and chat webhook addresses. Run it on a
real log file before the first launch and after a change to what is logged (docs/OWNER_RUNS.md).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[email]"),
    (
        "webhook-url",
        re.compile(
            r"https://(?:hooks\.slack\.com/services/|discord(?:app)?\.com/api/(?:v\d+/)?webhooks/"
            r"|[a-z0-9.\-]+\.webhook\.office\.com/)\S+",
            re.IGNORECASE,
        ),
        "[address]",
    ),
    (
        "url-credentials",
        re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:[^\s@/]+@", re.IGNORECASE),
        "[address]",
    ),
    (
        "url-query",
        re.compile(r"\bhttps?://[^\s?#]+\?\S+", re.IGNORECASE),
        "[address]",
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"),
        "[token]",
    ),
    ("bearer", re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=\-]{12,}", re.IGNORECASE), "[token]"),
    ("provider-key", re.compile(r"\bsk-(?:or-v1-|lf-)?[A-Za-z0-9\-]{16,}"), "[key]"),
    (
        "secret-assignment",
        re.compile(
            r"(?:api[_\-]?key|secret|token|passw(?:or)?d|authorization)[\"']?\s*[:=]\s*[\"']?"
            r"[^\s\"',;]{8,}",
            re.IGNORECASE,
        ),
        "[secret]",
    ),
    ("pan", re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"), "[id]"),
    ("indian-mobile", re.compile(r"(?<!\d)(?:\+?91[\s\-]?)?[6-9]\d{9}(?!\d)"), "[phone]"),
    ("long-number", re.compile(r"(?<![\w.])\d{9,}(?![\w.])"), "[number]"),
)


@dataclass(frozen=True)
class Finding:
    rule: str
    line: int


def scrub(text: str) -> str:
    """The text with every match of a rule replaced by a fixed word."""
    for _, pattern, word in RULES:
        text = pattern.sub(word, text)
    return text


def scan_text(text: str) -> list[Finding]:
    found: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern, _ in RULES:
            if pattern.search(line):
                found.append(Finding(rule, number))
    return found


def scan_file(path: Path | str) -> list[Finding]:
    return scan_text(Path(path).read_text(encoding="utf-8", errors="replace"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.observability.scan",
        allow_abbrev=False,
        description="Look for e-mail addresses, numbers, tokens, keys and addresses with "
        "credentials in a log or trace file. Prints rule names and line numbers only.",
    )
    parser.add_argument("file", help="the log or trace file to read")
    parser.add_argument("--max", type=int, default=50, help="most findings to list (default 50)")
    args = parser.parse_args(argv)
    path = Path(args.file)
    if not path.is_file():
        print("error: no such file", file=sys.stderr)
        return 2
    findings = scan_file(path)
    for item in findings[: max(args.max, 0)]:
        print(f"line {item.line}: {item.rule}")
    if len(findings) > args.max:
        print(f"... and {len(findings) - args.max} more")
    if not findings:
        print("nothing found")
        return 0
    print(f"{len(findings)} finding(s) in {path.name}: nothing was printed from the file")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
