#!/usr/bin/env python3
"""PreToolUse secret guard for Claude Code (spec build rule 2).

Blocks tool calls that would read, write or print secrets. Standard library
only. Exit 0 = no decision (the normal permission flow applies). Exit 2 =
block; the one-line stderr message goes back to the model. Fail closed: any
error inside this script blocks the call.

The script never opens or reads any file. Its message names the rule and the
tool only, never a path, a command or any content.
"""
import fnmatch
import json
import os
import posixpath
import re
import sys

MAX_SCAN_CHARS = 2_000_000

EXAMPLE_NAME = ".env.example"
SECRET_EXTENSIONS = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
SECRET_FILE_NAMES = {".netrc", ".pgpass"}
SECRET_DIR_NAMES = {"secrets", ".secrets"}
SSH_KEY_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
WILDCARD_SAMPLES = (
    ".env", ".env.local", ".env.production", ".env.eval",
    ".env.development", ".env.staging", "x.pem", "x.key", "x.p12",
    "credentials",
)

PLACEHOLDER_WORDS = (
    "your", "example", "placeholder", "changeme", "change-me", "change_me",
    "dummy", "fake", "sample", "redacted", "todo", "tbd", "xxxx", "****",
    "...", "<", ">", "${", "$(", "{{", "%", "os.environ", "getenv",
    "environ[", "key_here", "token_here",
)

# Prefixes verified in the providers' official documentation:
#   OpenRouter "sk-or-v1-", Langfuse secret key "sk-lf-". Length minimums are
#   a heuristic. No other provider prefix is used; everything else is generic.
OPENROUTER_KEY = re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}")
LANGFUSE_SECRET = re.compile(r"\bsk-lf-[A-Za-z0-9\-]{16,}")
PEM_PRIVATE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
BEARER = re.compile(r"\bbearer\s+([A-Za-z0-9._~+/=\-]{20,})", re.IGNORECASE)
URL_PASSWORD = re.compile(
    r"\b[a-z][a-z0-9+.\-]*://[^\s:/@\"'<>]+:([^\s@/\"'<>]{6,})@", re.IGNORECASE
)
ASSIGNMENT = re.compile(
    r"(?:api[_\-]?keys?|secret|token|passw(?:or)?d|pwd|credential"
    r"|private[_\-]?key|access[_\-]?key)[\w.\-]*[\"']?\s*[:=]\s*[\"']?"
    r"([^\s\"'#;&|<>,]{16,})",
    re.IGNORECASE,
)

SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|&\n]")
BARE_ENV_DUMP = re.compile(
    r"(?:cmd(?:\.exe)?\s+/c\s+)?(?:env|printenv|set|export)(?:\s+-\w+)?"
)
PS_ENV_DRIVE = re.compile(
    r"\b(?:get-childitem|gci|ls|dir|get-item|gi|get-content|gc|cat|type"
    r"|get-variable|gv)\b[^|;&\n]*\benv:"
)
ENV_API = re.compile(
    r"os\.environ|os\.getenv|environ\.get|\[environment\]::getenvironmentvariable"
)
PS_SECRET_ENV = re.compile(r"\$\{?env:\w*(?:key|secret|token|passw|credential)")
SHELL_SECRET_VAR = re.compile(r"\$\{?\w*(?:KEY|SECRET|TOKEN|PASSW|CREDENTIAL)")
TOKEN_SPLIT = re.compile(r"[\s\"'`;|&<>(),={}\[\]:]+")

PATH_FIELDS = {
    "Read": ("file_path",),
    "Edit": ("file_path",),
    "Write": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Grep": ("path", "glob"),
    "Glob": ("path", "pattern"),
}
CONTENT_FIELDS = {
    "Write": ("content",),
    "Edit": ("new_string",),
    "NotebookEdit": ("new_source",),
}
SHELL_TOOLS = ("Bash", "PowerShell")


def _normalise(raw):
    p = raw.strip().strip("\"'`").replace("\\", "/")
    if p.startswith("//?/"):
        p = p[4:]
    p = re.sub(r"::\$[a-z_]+$", "", p, flags=re.IGNORECASE)  # NTFS data stream
    p = p.lower()
    if p:
        p = posixpath.normpath(p)
    return p.rstrip(". ")


def _is_secret_path(raw):
    parts = [s for s in _normalise(raw).split("/") if s]
    if not parts:
        return False
    name = parts[-1]
    if any(part in SECRET_DIR_NAMES for part in parts):
        return True
    if name == EXAMPLE_NAME:
        return False
    return (
        name == ".env"
        or name.startswith(".env.")
        or name.endswith(".env")
        or name.endswith(SECRET_EXTENSIONS)
        or name in SECRET_FILE_NAMES
        or name.startswith("credentials")
        or (name.startswith(SSH_KEY_PREFIXES) and not name.endswith(".pub"))
    )


def _path_is_blocked(raw):
    if _is_secret_path(raw):
        return True
    try:
        real = os.path.realpath(raw)  # resolves links; does not read content
    except (OSError, ValueError):
        return True
    return _is_secret_path(real)


def _wildcard_hits_secret(pattern):
    last = posixpath.basename(pattern.replace("\\", "/").lower().rstrip("/"))
    if len(re.sub(r"[*?]", "", last)) < 2:
        return False
    return any(fnmatch.fnmatchcase(sample, last) for sample in WILDCARD_SAMPLES)


def _value_hits_secret_path(value):
    if "*" in value or "?" in value:
        segments = value.replace("\\", "/").lower().split("/")
        if any(seg in SECRET_DIR_NAMES for seg in segments[:-1]):
            return True
        return _wildcard_hits_secret(value)
    return _path_is_blocked(value)


def _is_placeholder(value):
    v = value.lower()
    if not v or v[0] in "$@" or len(set(v)) <= 3:
        return True
    return any(word in v for word in PLACEHOLDER_WORDS)


def _looks_like_secret(value):
    if _is_placeholder(value):
        return False
    if re.fullmatch(r"[0-9a-fA-F]{32,}", value):
        return True
    has_letter = re.search(r"[A-Za-z]", value) is not None
    has_digit = re.search(r"\d", value) is not None
    return has_letter and has_digit and not re.search(r"[()\[\]{}]", value)


def scan_text(text):
    """Return (rule, line_number) for the first secret-looking value, or None."""
    for number, line in enumerate(text[:MAX_SCAN_CHARS].splitlines(), start=1):
        if PEM_PRIVATE.search(line):
            return "private-key-block", number
        for rule, pattern in (
            ("openrouter-key", OPENROUTER_KEY),
            ("langfuse-secret-key", LANGFUSE_SECRET),
        ):
            match = pattern.search(line)
            if match and not _is_placeholder(match.group(0)):
                return rule, number
        for match in BEARER.finditer(line):
            if _looks_like_secret(match.group(1)):
                return "bearer-token", number
        for match in URL_PASSWORD.finditer(line):
            password = match.group(1)
            if not _is_placeholder(password) and (
                len(password) >= 20 or _looks_like_secret(password)
            ):
                return "url-credentials", number
        for match in ASSIGNMENT.finditer(line):
            if _looks_like_secret(match.group(1)):
                return "assigned-secret-value", number
    return None


def command_rule(command):
    low = command.lower()
    for segment in SEGMENT_SPLIT.split(low):
        s = segment.strip()
        if BARE_ENV_DUMP.fullmatch(s) or s.startswith("printenv "):
            return "environment-dump"
    if PS_ENV_DRIVE.search(low) or ENV_API.search(low):
        return "environment-read"
    if PS_SECRET_ENV.search(low) or SHELL_SECRET_VAR.search(command):
        return "secret-variable-reference"
    for token in TOKEN_SPLIT.split(command):
        if token and token.lower() not in SECRET_DIR_NAMES:
            if _value_hits_secret_path(token):
                return "secret-path-in-command"
    return None


def decide(payload):
    """Return None to allow, or (rule, line_number_or_None) to block."""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool, str) or not isinstance(tool_input, dict):
        return "bad-input", None
    for field in PATH_FIELDS.get(tool, ()):
        value = tool_input.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            return "bad-input", None
        if _value_hits_secret_path(value):
            return "secret-path", None
    for field in CONTENT_FIELDS.get(tool, ()):
        value = tool_input.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            return "bad-input", None
        hit = scan_text(value)
        if hit:
            return hit
    if tool in SHELL_TOOLS:
        command = tool_input.get("command")
        if not isinstance(command, str):
            return "bad-input", None
        rule = command_rule(command)
        if rule:
            return rule, None
        return scan_text(command)
    return None


def _block(rule, tool, line):
    safe_tool = re.sub(r"[^A-Za-z]", "", str(tool))[:20] or "unknown"
    where = f", line {line}" if line else ""
    message = (
        f"secret-guard: blocked by rule '{rule}' (tool {safe_tool}{where}). "
        "No secret values or secret files. Use placeholders in .env.example "
        "and ask the owner if access is truly needed.\n"
    )
    sys.stderr.buffer.write(message.encode("utf-8"))
    sys.stderr.buffer.flush()
    sys.exit(2)


def main():
    tool = "unknown"
    result = None
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        tool = payload.get("tool_name", "unknown")
        result = decide(payload)
    except Exception:
        _block("internal-error", tool, None)
    if result:
        _block(result[0], tool, result[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
