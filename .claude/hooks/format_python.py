#!/usr/bin/env python3
"""PostToolUse formatter for Claude Code.

After an Edit or Write, formats the Python file with `ruff format` and nothing
else. Only files under app/ and tests/ are touched, never .claude/, spec/ or
.venv/. Standard library only. Always exits 0 and prints nothing: a formatting
problem must never block or fail a tool call.

Usage: python format_python.py <project-root>   (the tool call JSON is on stdin)
"""
import json
import os
import subprocess
import sys

ALLOWED_TOP_DIRS = ("app", "tests")
BLOCKED_PARTS = {".claude", ".venv", "spec", "__pycache__", ".git"}
TIMEOUT_SECONDS = 20


def _venv_python(root):
    for relative in (".venv/Scripts/python.exe", ".venv/bin/python"):
        candidate = os.path.join(root, *relative.split("/"))
        if os.path.isfile(candidate):
            return candidate
    return None


def _target_file(root, file_path):
    """Return the real path to format, or None if the file must be left alone."""
    root_real = os.path.realpath(root)
    path = file_path if os.path.isabs(file_path) else os.path.join(root_real, file_path)
    real = os.path.realpath(path)
    try:
        relative = os.path.relpath(real, root_real)
    except ValueError:  # another drive on Windows
        return None
    parts = [part for part in relative.replace("\\", "/").split("/") if part]
    if not parts or parts[0] == ".." or os.path.isabs(relative):
        return None
    if parts[0].lower() not in ALLOWED_TOP_DIRS:
        return None
    if any(part.lower() in BLOCKED_PARTS for part in parts):
        return None
    if not parts[-1].lower().endswith(".py") or not os.path.isfile(real):
        return None
    return real


def main():
    try:
        root = sys.argv[1]
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
        payload = json.loads(raw)
        if payload.get("tool_name") not in ("Edit", "Write"):
            return
        file_path = (payload.get("tool_input") or {}).get("file_path")
        if not isinstance(file_path, str):
            return
        target = _target_file(root, file_path)
        python = _venv_python(root)
        if target is None or python is None:
            return
        subprocess.run(
            [python, "-m", "ruff", "format", "--force-exclude", "--", target],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TIMEOUT_SECONDS,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass  # fail open: formatting is a convenience, never a gate


if __name__ == "__main__":
    main()
    sys.exit(0)
