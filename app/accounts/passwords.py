"""Passwords: the rule, hashing and checking (spec 13.1, password rule [P]).

Rule: 10 to 128 characters, counted after Unicode NFKC normalisation (which is also applied
before hashing, so the same password typed in two forms is the same password); no forced
character classes; must not contain the username or the email name; must not be on a short list
of very common passwords. Hashing is argon2id with the OWASP minimum memory (19 MiB, t=2, p=1) so
it fits a small free host.

Hashing is deliberately slow and uses a lot of memory. Callers must run it off the event loop:
the auth routes are plain `def` routes, which FastAPI runs in a worker thread.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LENGTH = 10
MAX_LENGTH = 128
MIN_NAME_LENGTH = 3  # shorter names would match inside almost any password

_HASHER = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)

# Very common passwords of the minimum length or more, compared case-insensitively. The rule's
# length floor already removes the shorter ones.
COMMON_PASSWORDS = frozenset(
    {
        "1234567890",
        "0123456789",
        "12345678910",
        "123456789012",
        "1234567890123",
        "0987654321",
        "1111111111",
        "0000000000",
        "9876543210",
        "password123",
        "password1234",
        "password12345",
        "passw0rd123",
        "passw0rd1234",
        "password!23",
        "password@123",
        "password#123",
        "p@ssword123",
        "p@ssw0rd123",
        "pass1234567",
        "qwertyuiop",
        "qwertyuiop1",
        "qwerty12345",
        "qwerty123456",
        "qwertyuiop123",
        "asdfghjkl1",
        "asdfghjkl123",
        "zxcvbnm123",
        "1q2w3e4r5t",
        "1q2w3e4r5t6y",
        "1qaz2wsx3edc",
        "iloveyou123",
        "iloveyou1234",
        "letmein1234",
        "letmein12345",
        "welcome1234",
        "welcome12345",
        "welcome@123",
        "admin123456",
        "administrator",
        "administrator1",
        "changeme123",
        "changemenow",
        "abcdefghij",
        "abcd123456",
        "abc1234567",
        "abcabcabcabc",
        "monkey12345",
        "dragon12345",
        "football123",
        "baseball123",
        "sunshine123",
        "princess123",
        "superman123",
        "trustno1234",
        "mypassword1",
        "mypassword123",
        "bankpassword",
        "bankpassword1",
        "insurance123",
        "customer123",
        "customer1234",
        "india@12345",
        "india123456",
        "indiaindia1",
    }
)


def normalise_password(password: str) -> str:
    """Unicode NFKC: the same password in two compatible forms becomes one."""
    return unicodedata.normalize("NFKC", password)


def check_password_rule(password: str, *, username: str = "", email: str = "") -> list[str]:
    """The reasons a password is refused, as short codes (an empty list means it is fine).

    Codes: too_short, too_long, contains_username, contains_email_name, too_common, one_character.
    The password itself is never part of a code or a message.
    """
    text = normalise_password(password)
    problems: list[str] = []
    if len(text) < MIN_LENGTH:
        problems.append("too_short")
    if len(text) > MAX_LENGTH:
        problems.append("too_long")
    folded = text.casefold()
    name = normalise_password(username).strip().casefold()
    local = normalise_password(email).strip().casefold().split("@", 1)[0]
    if len(name) >= MIN_NAME_LENGTH and name in folded:
        problems.append("contains_username")
    if len(local) >= MIN_NAME_LENGTH and local in folded:
        problems.append("contains_email_name")
    if folded in COMMON_PASSWORDS:
        problems.append("too_common")
    if len(text) >= MIN_LENGTH and len(set(text)) == 1:
        problems.append("one_character")
    return problems


def hash_password(password: str) -> str:
    """The argon2id hash of the NFKC-normalised password. Slow: call it off the event loop."""
    return _HASHER.hash(normalise_password(password))


def verify_password(stored_hash: str, password: str) -> bool:
    """True only if the password matches. Any problem with the hash or the password is False."""
    if len(password) > 4 * MAX_LENGTH:  # cap the work an attacker can ask for
        return False
    try:
        return _HASHER.verify(stored_hash, normalise_password(password))
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True if the hash was made with weaker settings than the current ones."""
    try:
        return _HASHER.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """A real hash of a throw-away value, made once. Checking a login for a name that does not
    exist against it takes as long as checking a real one, so timing does not reveal which
    usernames exist."""
    return _HASHER.hash("a throw-away value that no user has")
