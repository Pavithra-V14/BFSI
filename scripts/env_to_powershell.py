"""Put the variables of your .env file into THIS PowerShell window, in one step. OWNER-RUN.

    uv run python scripts/env_to_powershell.py --emit | Invoke-Expression    # load them
    uv run python scripts/env_to_powershell.py --list                        # names only
    uv run python scripts/env_to_powershell.py --clear | Invoke-Expression   # remove them again

Why a pipe: a program cannot change its parent window's variables. This script prints the
PowerShell lines that set them (`$env:NAME = '...'`) and `Invoke-Expression` runs those lines in
your window. The lines hold the VALUES, so they are printed ONLY when you say `--emit` AND the
output goes into a pipe (never to the screen). Without `--emit` the script prints nothing of the
kind, whoever runs it. Your terminal shows the names it loaded (on the error stream, never a value).

Rules (the same restricted loader as `--load-env`, `scripts/envfile.py`):
- only names listed in `.env.example` are loaded;
- a variable already set in this window wins over the file;
- empty values, malformed lines and values with a line break or a non-ASCII character are skipped
  (the name is listed as skipped);
- a value is only ever written to the pipe, never to a file, a log or the screen.

The variables last for this window only. Do not run the tests in a window that holds your real
database, Qdrant or cache addresses: the tests refuse to start there (isolation guard). Close the
window, or run `--clear`, before the tests. Claude never runs this script and never reads the file.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.envfile import example_names, load_env_file  # noqa: E402


def powershell_quote(value: str) -> str:
    """A PowerShell single-quoted string: nothing inside it is interpreted, a quote is doubled."""
    return "'" + value.replace("'", "''") + "'"


def usable(value: str) -> bool:
    """A value that survives the pipe: printable ASCII only (no line break or control character)."""
    return all(32 <= ord(ch) < 127 for ch in value)


def new_values(
    env_path: Path, example_path: Path, environ: Mapping[str, str]
) -> tuple[dict[str, str], list[str]]:
    """(the variables the file would add, the names skipped for an unusable value). Variables
    already set in `environ` are not touched."""
    scratch: MutableMapping[str, str] = dict(environ)
    names = load_env_file(env_path, example_names(example_path), scratch)
    found: dict[str, str] = {}
    skipped: list[str] = []
    for name in names:
        if usable(scratch[name]):
            found[name] = scratch[name]
        else:
            skipped.append(name)
    return found, skipped


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    out: Callable[[str], None] | None = None,
    err: Callable[[str], None] | None = None,
    stdout_is_screen: Callable[[], bool] | None = None,
    env_path: Path | None = None,
    example_path: Path | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="env_to_powershell", description=__doc__.split("\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="show the names that would load")
    mode.add_argument("--clear", action="store_true", help="print lines that remove them again")
    parser.add_argument(
        "--emit", action="store_true", help="print the lines that set the variables (into a pipe)"
    )
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    out = out or sys.stdout.write
    err = err or (lambda text: print(text, file=sys.stderr))
    on_screen = stdout_is_screen or (lambda: sys.stdout.isatty())
    env_path = env_path or ROOT / ".env"
    example_path = example_path or ROOT / ".env.example"

    if args.clear:  # names only: safe to show
        for name in sorted(example_names(example_path)):
            out(f"Remove-Item Env:{name} -ErrorAction SilentlyContinue\n")
        err("clear: remove lines printed (names only); pipe them into Invoke-Expression")
        return 0
    if not env_path.is_file():
        err("no .env file found in the project folder: nothing to load")
        return 1
    found, skipped = new_values(env_path, example_path, environ)
    if args.list:
        err("would load: " + (", ".join(sorted(found)) or "none") + " (values are never shown)")
        if skipped:
            err(
                "skipped (a value with a line break or a non-ASCII character): "
                + ", ".join(skipped)
            )
        return 0
    if not args.emit or on_screen():
        err(
            "refused: this prints the VALUES, so it needs --emit and must go into a pipe, never "
            "to the screen. Run it as:\n"
            "  uv run python scripts/env_to_powershell.py --emit | Invoke-Expression"
        )
        return 2
    for name in sorted(found):
        out(f"$env:{name} = {powershell_quote(found[name])}\n")
    err(
        f"loaded {len(found)} variable(s) into this window: " + (", ".join(sorted(found)) or "none")
    )
    if skipped:
        err("skipped (a value with a line break or a non-ASCII character): " + ", ".join(skipped))
    err("variables already set in this window were left as they are (they win over the file)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
