"""One fixed rule for usernames and email addresses: strip, then casefold.

The database does not fold case (lower() differs between Postgres and SQLite), so every place
that stores or looks up a username or an email must call these first. Python's casefold() is the
same on every machine, so a name normalised here compares equal however it was typed.
"""

from __future__ import annotations


def normalise_identity(value: str) -> str:
    """Strip surrounding whitespace, then casefold."""
    return value.strip().casefold()


def normalise_username(value: str) -> str:
    return normalise_identity(value)


def normalise_email(value: str) -> str:
    return normalise_identity(value)
