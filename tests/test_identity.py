"""One fixed rule for usernames and emails: strip, then casefold (app/db/identity.py)."""

import pytest

from app.db.identity import normalise_email, normalise_identity, normalise_username


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("asha", "asha"),
        ("  Asha ", "asha"),
        ("ASHA", "asha"),
        ("\tAsha\n", "asha"),
        ("Asha.Rao@Example.TEST", "asha.rao@example.test"),
        (" ASHA@EXAMPLE.TEST ", "asha@example.test"),
        ("Straße", "strasse"),  # casefold, not just lower(): the same on every machine
        ("STRASSE", "strasse"),
        ("Émile", "émile"),
        ("İstanbul", "i̇stanbul"),  # a capital dotted I folds the same way everywhere
        ("", ""),
        ("   ", ""),
    ],
)
def test_strip_then_casefold(typed, stored):
    assert normalise_identity(typed) == stored


def test_username_and_email_use_the_same_rule():
    for value in ("  Mixed Case ", "ÀÉÎ", "Straße"):
        assert normalise_username(value) == normalise_email(value) == normalise_identity(value)


def test_the_rule_is_idempotent():
    for value in ("  Asha ", "Straße", "İstanbul", "ÀÉÎ@Example.TEST"):
        once = normalise_identity(value)
        assert normalise_identity(once) == once


def test_stripping_happens_before_folding_and_only_at_the_ends():
    assert normalise_identity("  a b  ") == "a b"  # inner spaces are kept
