"""Login tokens: expiry, signature, algorithm, revocation and the purge (spec 13.1)."""

import base64
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.accounts import tokens
from app.accounts.tokens import (
    InvalidToken,
    decode_token,
    is_revoked,
    issue_token,
    purge_expired_revocations,
    revoke,
    utc_text,
)
from tests.api_fixtures import (  # noqa: F401
    AUDIT_KEY,
    JWT_SECRET,
    OTHER_SECRET,
    make_settings,
    new_id,
)

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def part(token, index):
    raw = token.split(".")[index]
    return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


def forge(header, payload, signature=""):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{encode(header)}.{encode(payload)}.{signature}"


def good_payload(**changes):
    payload = {
        "sub": "user-1",
        "jti": "jti-1",
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(hours=1)).timestamp()),
    }
    payload.update(changes)
    return payload


def test_a_token_round_trips_and_carries_only_the_four_claims():
    settings = make_settings()
    token, claims = issue_token(settings, "user-1", NOW)
    assert part(token, 0) == {"alg": "HS256", "typ": "JWT"}
    assert set(part(token, 1)) == {"sub", "jti", "iat", "exp"}  # no role, no name, no email
    decoded = decode_token(settings, token, now=NOW + timedelta(minutes=5))
    assert decoded.user_id == "user-1" and decoded.jti == claims.jti
    assert decoded.expires_at == NOW + timedelta(minutes=settings.jwt_expire_minutes)


def test_the_expiry_follows_the_setting():
    token, claims = issue_token(make_settings(jwt_expire_minutes=15), "u", NOW)
    assert claims.expires_at == NOW + timedelta(minutes=15)
    assert part(token, 1)["exp"] - part(token, 1)["iat"] == 15 * 60


def test_every_token_has_its_own_id():
    settings = make_settings()
    assert issue_token(settings, "u", NOW)[1].jti != issue_token(settings, "u", NOW)[1].jti


def test_a_token_is_refused_from_its_expiry_on():
    settings = make_settings(jwt_expire_minutes=10)
    token, claims = issue_token(settings, "u", NOW)
    decode_token(settings, token, now=claims.expires_at - timedelta(seconds=1))
    for later in (claims.expires_at, claims.expires_at + timedelta(days=1)):
        with pytest.raises(InvalidToken) as caught:
            decode_token(settings, token, now=later)
        assert caught.value.reason == "expired"


def test_a_token_signed_with_another_secret_is_refused():
    other = make_settings(jwt_secret=OTHER_SECRET)
    token, _ = issue_token(other, "u", NOW)
    with pytest.raises(InvalidToken):
        decode_token(make_settings(), token, now=NOW)


def test_a_changed_payload_is_refused():
    settings = make_settings()
    token, _ = issue_token(settings, "user-1", NOW)
    header, payload, signature = token.split(".")
    forged = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    forged["sub"] = "admin-1"
    swapped = base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode()
    with pytest.raises(InvalidToken):
        decode_token(settings, f"{header}.{swapped}.{signature}", now=NOW)


def test_a_token_with_algorithm_none_is_refused():
    token = forge({"alg": "none", "typ": "JWT"}, good_payload())
    with pytest.raises(InvalidToken):
        decode_token(make_settings(), token, now=NOW)
    with pytest.raises(InvalidToken):
        decode_token(make_settings(), token + "x", now=NOW)


@pytest.mark.parametrize("algorithm", ["HS384", "HS512"])
def test_a_token_with_another_algorithm_is_refused_even_with_the_right_secret(algorithm):
    token = jwt.encode(good_payload(), JWT_SECRET, algorithm=algorithm)
    with pytest.raises(InvalidToken):
        decode_token(make_settings(), token, now=NOW)


@pytest.mark.parametrize("missing", ["sub", "jti", "iat", "exp"])
def test_a_token_missing_a_required_claim_is_refused(missing):
    payload = good_payload()
    del payload[missing]
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    with pytest.raises(InvalidToken):
        decode_token(make_settings(), token, now=NOW)


@pytest.mark.parametrize(
    "changes", [{"sub": ""}, {"jti": ""}, {"sub": 123}, {"exp": "tomorrow"}, {"iat": "now"}]
)
def test_a_token_with_a_malformed_claim_is_refused(changes):
    token = jwt.encode(good_payload(**changes), JWT_SECRET, algorithm="HS256")
    with pytest.raises(InvalidToken):
        decode_token(make_settings(), token, now=NOW)


def test_a_token_issued_in_the_future_is_refused():
    future = good_payload(iat=int((NOW + timedelta(hours=1)).timestamp()))
    future["exp"] = int((NOW + timedelta(hours=2)).timestamp())
    token = jwt.encode(future, JWT_SECRET, algorithm="HS256")
    with pytest.raises(InvalidToken):
        decode_token(make_settings(), token, now=NOW)


@pytest.mark.parametrize("garbage", ["", "x", "a.b.c", "Bearer abc", "....", "éé"])
def test_garbage_is_refused_and_never_raises_anything_else(garbage):
    with pytest.raises(InvalidToken):
        decode_token(make_settings(), garbage, now=NOW)


def test_the_error_carries_a_code_and_nothing_of_the_token():
    with pytest.raises(InvalidToken) as caught:
        decode_token(make_settings(), "abc.def.ghi", now=NOW)
    assert str(caught.value) == "invalid" and caught.value.__cause__ is None


# ---- revocation -------------------------------------------------------------------------------


def seed_user(db):
    user_id = new_id()
    db.execute(
        "INSERT INTO users (id, username, email, password_hash) VALUES (?, ?, ?, 'h')",
        (user_id, "asha_rao", "asha@example.test"),
    )
    return user_id


def claims_for(user_id, jti="jti-a", hours=1):
    return tokens.Claims(user_id, jti, NOW, NOW + timedelta(hours=hours))


def test_a_revoked_token_is_found_and_another_is_not(db):
    user = seed_user(db)
    revoke(db, claims_for(user, "jti-a"), NOW)
    assert is_revoked(db, "jti-a") is True
    assert is_revoked(db, "jti-b") is False


def test_revoking_twice_is_harmless(db):
    user = seed_user(db)
    revoke(db, claims_for(user), NOW)
    revoke(db, claims_for(user), NOW + timedelta(minutes=1))
    assert db.query_one("SELECT COUNT(*) FROM revoked_tokens") == (1,)


def test_a_revoked_row_keeps_the_tokens_own_expiry(db):
    user = seed_user(db)
    revoke(db, claims_for(user, hours=3), NOW)
    row = db.query_one("SELECT revoked_at, expires_at FROM revoked_tokens")
    assert row == (utc_text(NOW), utc_text(NOW + timedelta(hours=3)))


def test_times_are_written_in_one_shape_with_a_utc_offset():
    local = datetime(2026, 10, 6, 17, 30, tzinfo=timezone_plus_five_thirty())
    assert utc_text(local) == "2026-10-06T12:00:00.000000+00:00"
    assert utc_text(NOW) == "2026-10-06T12:00:00.000000+00:00"


def timezone_plus_five_thirty():
    from datetime import timezone

    return timezone(timedelta(hours=5, minutes=30))


def test_the_purge_deletes_expired_rows_and_keeps_the_rest(db):
    user = seed_user(db)
    revoke(db, claims_for(user, "gone-1", hours=-2), NOW - timedelta(hours=5))  # expired 2 h ago
    revoke(db, claims_for(user, "gone-2", hours=-1), NOW - timedelta(hours=5))
    revoke(db, claims_for(user, "keep-1", hours=1), NOW)  # still valid for an hour
    revoke(db, claims_for(user, "keep-2", hours=48), NOW)
    assert purge_expired_revocations(db, NOW) == 2
    remaining = {row[0] for row in db.query_all("SELECT jti FROM revoked_tokens")}
    assert remaining == {"keep-1", "keep-2"}


def test_the_purge_boundary_keeps_a_token_that_expires_this_instant_for_one_more_tick(db):
    user = seed_user(db)
    revoke(db, claims_for(user, "edge", hours=0), NOW)  # expires exactly at NOW
    assert purge_expired_revocations(db, NOW) == 0  # not yet strictly in the past
    assert purge_expired_revocations(db, NOW + timedelta(microseconds=1)) == 1


def test_the_purge_on_an_empty_table_is_zero_and_repeatable(db):
    assert purge_expired_revocations(db, NOW) == 0
    assert purge_expired_revocations(db, NOW) == 0


def test_the_purge_orders_times_correctly_with_and_without_fractions(db):
    user = seed_user(db)
    exact = tokens.Claims(user, "exact", NOW, NOW.replace(microsecond=0))
    fraction = tokens.Claims(user, "fraction", NOW, NOW.replace(microsecond=500_000))
    revoke(db, exact, NOW)
    revoke(db, fraction, NOW)
    cutoff = NOW.replace(microsecond=250_000)
    assert purge_expired_revocations(db, cutoff) == 1  # only the one that expired before it
    assert {row[0] for row in db.query_all("SELECT jti FROM revoked_tokens")} == {"fraction"}


def test_a_purged_token_is_still_refused_because_it_expired(db):
    settings = make_settings(jwt_expire_minutes=10)
    user = seed_user(db)
    token, claims = issue_token(settings, user, NOW)
    revoke(db, claims, NOW)
    later = NOW + timedelta(hours=2)
    purge_expired_revocations(db, later)
    assert not is_revoked(db, claims.jti)  # the row is gone
    with pytest.raises(InvalidToken):  # but the token is refused by its expiry
        decode_token(settings, token, now=later)
