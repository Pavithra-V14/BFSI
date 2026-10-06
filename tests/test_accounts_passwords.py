"""The password rule and hashing (spec 13.1, password rule [P])."""

import time

import pytest

from app.accounts.passwords import (
    COMMON_PASSWORDS,
    MAX_LENGTH,
    MIN_LENGTH,
    check_password_rule,
    dummy_hash,
    hash_password,
    needs_rehash,
    normalise_password,
    verify_password,
)

GOOD = "correct horse battery staple"


def test_a_good_password_has_no_problems():
    assert check_password_rule(GOOD, username="asha_rao", email="asha_rao@example.test") == []


def test_the_length_limits_count_characters():
    assert MIN_LENGTH == 10 and MAX_LENGTH == 128
    assert check_password_rule("x7Kq-9mZp") == ["too_short"]  # 9 characters
    assert check_password_rule("x7Kq-9mZp2") == []  # 10 characters
    assert check_password_rule("ab" * 64 + "q") == ["too_long"]  # 129 characters
    assert check_password_rule(("Ab3" * 42) + "xy") == []  # 128 characters


def test_a_character_is_counted_once_whatever_its_byte_size():
    emoji = "".join(chr(0x1F600 + offset) for offset in range(10))  # ten emoji
    assert len(emoji) == 10 and len(emoji.encode()) == 40
    assert "too_short" not in check_password_rule(emoji)
    assert "too_short" in check_password_rule(emoji[:9])


def test_length_is_counted_after_nfkc_normalisation():
    # A ligature "ﬁ" is one character that NFKC turns into the two characters "fi".
    password = "ﬁ" * 5  # 5 characters before, 10 after
    assert len(password) == 5 and len(normalise_password(password)) == 10
    assert "too_short" not in check_password_rule(password)


def test_full_width_forms_are_normalised_before_the_common_password_check():
    full_width = "ｐａｓｓｗｏｒｄ１２３"
    assert normalise_password(full_width) == "password123"
    assert "too_common" in check_password_rule(full_width)


@pytest.mark.parametrize("password", sorted(COMMON_PASSWORDS)[:10] + ["PASSWORD123", "Qwertyuiop"])
def test_a_common_password_is_refused_whatever_its_case(password):
    assert "too_common" in check_password_rule(password)


def test_every_listed_common_password_is_long_enough_to_matter():
    assert all(len(item) >= MIN_LENGTH - 1 for item in COMMON_PASSWORDS)
    assert len(COMMON_PASSWORDS) >= 50


def test_the_username_may_not_appear_in_the_password():
    problems = check_password_rule("xx-asha_rao-xx-123", username="asha_rao")
    assert problems == ["contains_username"]
    assert check_password_rule("xx-ASHA_RAO-xx-123", username="asha_rao") == ["contains_username"]


def test_the_email_name_may_not_appear_in_the_password():
    problems = check_password_rule("my-ravikumar-secret-1", email="ravikumar@example.test")
    assert problems == ["contains_email_name"]
    assert check_password_rule("my-example-secret-1", email="ravikumar@example.test") == []


def test_a_very_short_name_is_not_matched_inside_passwords():
    assert (
        check_password_rule("a-long-enough-passphrase", username="al", email="al@example.test")
        == []
    )


def test_one_repeated_character_is_refused():
    assert "one_character" in check_password_rule("z" * 12)


def test_forced_character_classes_are_not_required():
    assert check_password_rule("only lowercase letters here") == []
    assert check_password_rule("ONLY UPPERCASE LETTERS HERE") == []


def test_the_codes_never_contain_the_password():
    codes = check_password_rule("secret-asha_rao-pw", username="asha_rao")
    assert all("secret" not in code and "asha_rao" not in code for code in codes)


# ---- hashing ---------------------------------------------------------------------------------


def test_the_real_settings_are_argon2id_at_the_owasp_minimum():
    stored = hash_password(GOOD)
    assert stored.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert GOOD not in stored


def test_a_hash_verifies_only_the_right_password():
    stored = hash_password(GOOD)
    assert verify_password(stored, GOOD) is True
    assert verify_password(stored, GOOD + "x") is False
    assert verify_password(stored, "") is False


def test_two_hashes_of_one_password_differ_by_their_salt():
    assert hash_password(GOOD) != hash_password(GOOD)


def test_the_same_password_in_two_unicode_forms_is_one_password():
    composed, decomposed = "café au lait 42", "café au lait 42"
    assert composed != decomposed
    stored = hash_password(composed)
    assert verify_password(stored, decomposed) is True
    full_width = "ｃａｆé au lait 42"
    assert verify_password(stored, full_width) is True


@pytest.mark.parametrize("bad", ["", "not a hash", "$argon2id$garbage", "$2b$12$abcdefghijklmnop"])
def test_a_broken_stored_hash_never_matches_and_never_raises(bad):
    assert verify_password(bad, GOOD) is False


def test_an_enormous_password_is_refused_without_the_work_of_hashing_it():
    stored = hash_password(GOOD)
    started = time.perf_counter()
    assert verify_password(stored, "x" * 1_000_000) is False
    assert time.perf_counter() - started < 0.2


def test_a_hash_made_with_weaker_settings_needs_a_rehash():
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(GOOD)
    assert needs_rehash(weak) is True
    assert needs_rehash(hash_password(GOOD)) is False
    assert needs_rehash("not a hash") is True


def test_the_dummy_hash_is_a_real_hash_made_once_that_matches_nothing_useful():
    first = dummy_hash()
    assert first.startswith("$argon2id$") and dummy_hash() is first
    assert verify_password(first, GOOD) is False
