"""API settings: the secrets rules, the numbers and the refusal to start (spec 13.1, 13.3)."""

import pytest

from app.api.main import create_app_from_env
from app.api.settings import (
    DEFAULT_JWT_EXPIRE_MINUTES,
    MIN_SECRET_CHARACTERS,
    ApiSettings,
    SettingsError,
    check_origin,
    check_secret,
    keys_are_related,
)
from tests.api_fixtures import AUDIT_KEY, JWT_SECRET, OTHER_SECRET, make_settings


def test_the_fake_secrets_used_by_the_tests_pass_the_rules_and_differ():
    assert len(JWT_SECRET) >= MIN_SECRET_CHARACTERS and len(AUDIT_KEY) >= MIN_SECRET_CHARACTERS
    assert JWT_SECRET != AUDIT_KEY
    settings = make_settings()
    assert settings.jwt_secret == JWT_SECRET


@pytest.mark.parametrize("name", ["JWT_SECRET_KEY", "AUDIT_HASH_KEY"])
def test_an_empty_secret_is_refused(name):
    with pytest.raises(SettingsError, match=f"{name} is not set"):
        check_secret("", name)


@pytest.mark.parametrize("length", [1, 10, 32, 42])
def test_a_short_secret_is_refused_and_its_value_never_shown(length):
    value = "#" * length  # a character the message never contains
    with pytest.raises(SettingsError) as caught:
        check_secret(value, "JWT_SECRET_KEY")
    assert "too short" in str(caught.value) and value not in str(caught.value)


URLSAFE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"


def random_text(length, alphabet=URLSAFE, seed=11):
    import random

    rng = random.Random(seed)
    return "".join(rng.choice(alphabet) for _ in range(length))


def test_exactly_the_minimum_length_is_accepted():
    check_secret(random_text(MIN_SECRET_CHARACTERS), "JWT_SECRET_KEY")


def test_a_hex_key_from_openssl_is_accepted_every_time():
    import secrets

    for _ in range(300):  # a hex key has only 16 symbols: the old rule refused about a quarter
        check_secret(secrets.token_hex(32), "JWT_SECRET_KEY")
    check_secret(secrets.token_hex(32).upper(), "JWT_SECRET_KEY")


def test_a_random_url_safe_key_is_accepted_every_time():
    import secrets

    for _ in range(300):
        check_secret(secrets.token_urlsafe(32), "JWT_SECRET_KEY")


@pytest.mark.parametrize(
    "value",
    [
        "Xk3-9fQz_LmPv2Rt" * 3,  # a 16 character pattern three times
        "Xk3-9fQz_LmPv2Rt" * 3 + "Xk3-9",  # and a partial copy at the end
        "ab" * 25,
        "abc123XYZ" * 6,
        random_text(20) * 3,  # random, but then repeated
        "ab12" * 16,  # hex, few different digits, repeated
    ],
)
def test_a_repeated_pattern_is_refused(value):
    with pytest.raises(SettingsError, match="random") as caught:
        check_secret(value, "JWT_SECRET_KEY")
    assert value not in str(caught.value)


@pytest.mark.parametrize(
    "run",
    [
        "qwertyuiop",
        "asdfghjkl",
        "zxcvbnm",
        "abcdefghijklmnop",
        "0123456789",
        "9876543210",
        "poiuytrewq",
        "lkjhgfdsa",
        "QWERTYUIOP",
        "mnbvcxz",
        "zyxwvutsrqponm",
    ],
)
def test_a_keyboard_alphabet_or_digit_run_is_refused_wherever_it_sits(run):
    padding = random_text(40, seed=5)
    for value in (run + padding, padding[:20] + run + padding[20:], padding + run):
        with pytest.raises(SettingsError, match="random"):
            check_secret(value, "JWT_SECRET_KEY")


def test_a_short_run_inside_a_random_key_is_fine():
    value = random_text(30, seed=3) + "qwert" + random_text(20, seed=4)
    check_secret(value, "JWT_SECRET_KEY")


@pytest.mark.parametrize(
    "value",
    [
        "your-secret-key-goes-here-your-secret-key-goes-here",
        "Your_JWT_Secret_Here_Please_Replace_This_Value_Now",
        "changeme" * 6,
        "change-this-to-a-long-random-string-before-use-ok",
        "replace-me-with-something-random-and-long-enough-x",
        "example-secret-value-example-secret-value-example",
        "placeholder-placeholder-placeholder-placeholder-x",
        "<paste-your-secret-here-paste-your-secret-here-ok>",
        "${JWT_SECRET_KEY}${JWT_SECRET_KEY}${JWT_SECRET_KEY}",
        "a" * 50,
        "abababababababababababababababababababababababab",
        "0123012301230123012301230123012301230123012301230",
    ],
)
def test_a_placeholder_or_low_variety_secret_is_refused(value):
    with pytest.raises(SettingsError, match="placeholder") as caught:
        check_secret(value, "JWT_SECRET_KEY")
    assert value not in str(caught.value)


def test_the_two_keys_must_not_be_equal():
    with pytest.raises(SettingsError, match="unrelated keys"):
        ApiSettings(jwt_secret=JWT_SECRET, audit_hash_key=JWT_SECRET)


def test_one_key_may_not_contain_the_other():
    with pytest.raises(SettingsError, match="unrelated keys"):
        ApiSettings(jwt_secret=JWT_SECRET, audit_hash_key=JWT_SECRET + "-and-more-words-xyz")
    with pytest.raises(SettingsError, match="unrelated keys"):
        ApiSettings(jwt_secret=AUDIT_KEY + "-extra-tail-words", audit_hash_key=AUDIT_KEY)


def test_a_reversed_key_is_related():
    with pytest.raises(SettingsError, match="unrelated keys"):
        ApiSettings(jwt_secret=JWT_SECRET, audit_hash_key=JWT_SECRET[::-1])


def test_a_key_with_a_few_characters_changed_is_related():
    changed = list(JWT_SECRET)
    for position in (3, 10, 20, 30):
        changed[position] = "#"
    with pytest.raises(SettingsError, match="unrelated keys"):
        ApiSettings(jwt_secret=JWT_SECRET, audit_hash_key="".join(changed))


def test_a_key_built_from_a_piece_of_the_other_is_related():
    one = random_text(43, seed=21)
    other = random_text(20, seed=22) + one[10:30] + random_text(10, seed=23)
    assert keys_are_related(one, other)
    assert keys_are_related(one, other[::-1])
    assert keys_are_related(one.upper(), other.lower())  # case does not hide it


def test_two_independent_random_keys_are_unrelated():
    import secrets

    for _ in range(200):
        assert not keys_are_related(secrets.token_urlsafe(32), secrets.token_urlsafe(32))
        assert not keys_are_related(secrets.token_hex(32), secrets.token_urlsafe(32))


def test_the_fake_keys_used_by_the_tests_are_unrelated_to_each_other():
    assert not keys_are_related(JWT_SECRET, AUDIT_KEY)
    assert not keys_are_related(JWT_SECRET, OTHER_SECRET)
    assert not keys_are_related(AUDIT_KEY, OTHER_SECRET)


def test_a_real_looking_generated_secret_is_accepted():
    import secrets

    one, other = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    assert len(one) == 43
    ApiSettings(jwt_secret=one, audit_hash_key=other)


def test_settings_never_show_a_secret_in_repr():
    text = repr(make_settings())
    assert JWT_SECRET not in text and AUDIT_KEY not in text


def test_defaults_and_overrides():
    settings = make_settings()
    assert settings.jwt_expire_minutes == DEFAULT_JWT_EXPIRE_MINUTES == 480
    assert settings.cors_origin is None
    assert settings.daily_budget("customer") == 50_000
    assert settings.daily_budget("staff") == settings.daily_budget("admin") == 200_000
    custom = make_settings(token_budget_customer=10, token_budget_staff=99)
    assert (custom.daily_budget("customer"), custom.daily_budget("admin")) == (10, 99)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "480", 60 * 24 * 30 + 1])
def test_a_bad_expiry_is_refused(value):
    with pytest.raises(SettingsError, match="JWT_EXPIRE_MINUTES"):
        make_settings(jwt_expire_minutes=value)


@pytest.mark.parametrize("name", ["token_budget_customer", "token_budget_staff"])
@pytest.mark.parametrize("value", [0, -5, True, "100", 2.5])
def test_a_bad_budget_is_refused(name, value):
    with pytest.raises(SettingsError, match="TOKEN_BUDGET"):
        make_settings(**{name: value})


@pytest.mark.parametrize(
    "origin", ["https://app.example.test", "http://localhost:8501", "https://a.b.example.test:8443"]
)
def test_one_plain_origin_is_accepted(origin):
    check_origin(origin)


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "https://*.example.test",
        "https://app.example.test/",
        "https://app.example.test/path",
        "https://a.example.test,https://b.example.test",
        "app.example.test",
        "ftp://app.example.test",
        "https://",
        "https://app.example.test?x=1",
        "",
    ],
)
def test_a_wildcard_a_list_or_a_path_is_not_an_origin(origin):
    with pytest.raises(SettingsError, match="CORS_ALLOWED_ORIGIN"):
        check_origin(origin)


def test_from_env_reads_every_setting():
    settings = ApiSettings.from_env(
        {
            "JWT_SECRET_KEY": f"  {JWT_SECRET}  ",
            "AUDIT_HASH_KEY": AUDIT_KEY,
            "JWT_EXPIRE_MINUTES": "60",
            "CORS_ALLOWED_ORIGIN": "https://app.example.test",
            "TOKEN_BUDGET_CUSTOMER": "1000",
            "TOKEN_BUDGET_STAFF": "5000",
        }
    )
    assert settings.jwt_secret == JWT_SECRET and settings.jwt_expire_minutes == 60
    assert settings.cors_origin == "https://app.example.test"
    assert (settings.token_budget_customer, settings.token_budget_staff) == (1000, 5000)


def test_from_env_with_only_the_two_secrets_uses_the_defaults():
    settings = ApiSettings.from_env({"JWT_SECRET_KEY": JWT_SECRET, "AUDIT_HASH_KEY": AUDIT_KEY})
    assert settings.cors_origin is None and settings.jwt_expire_minutes == 480


@pytest.mark.parametrize(
    "name", ["JWT_EXPIRE_MINUTES", "TOKEN_BUDGET_CUSTOMER", "TOKEN_BUDGET_STAFF"]
)
def test_a_text_number_is_refused(name):
    env = {"JWT_SECRET_KEY": JWT_SECRET, "AUDIT_HASH_KEY": AUDIT_KEY, name: "soon"}
    with pytest.raises(SettingsError, match=name):
        ApiSettings.from_env(env)


# ---- the app refuses to start ------------------------------------------------------------------


def good_env(**changes):
    env = {
        "JWT_SECRET_KEY": JWT_SECRET,
        "AUDIT_HASH_KEY": AUDIT_KEY,
        "DATABASE_URL": "sqlite:///unused.db",
    }
    env.update(changes)
    return env


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        ({"JWT_SECRET_KEY": ""}, "JWT_SECRET_KEY"),
        ({"AUDIT_HASH_KEY": ""}, "AUDIT_HASH_KEY"),
        ({"JWT_SECRET_KEY": "short"}, "JWT_SECRET_KEY"),
        ({"AUDIT_HASH_KEY": "your-secret-goes-here-" * 3}, "AUDIT_HASH_KEY"),
        ({"AUDIT_HASH_KEY": JWT_SECRET}, "unrelated keys"),
        ({"DATABASE_URL": ""}, "DATABASE_URL"),
        ({"CORS_ALLOWED_ORIGIN": "*"}, "CORS_ALLOWED_ORIGIN"),
    ],
)
def test_the_app_refuses_to_start_on_a_bad_setting_and_names_it_without_a_value(changes, named):
    with pytest.raises(SystemExit) as caught:
        create_app_from_env(good_env(**changes))
    message = str(caught.value)
    assert message.startswith("refusing to start") and named in message
    for secret in (JWT_SECRET, AUDIT_KEY):
        assert secret not in message


def test_a_start_with_good_settings_builds_the_app():
    app = create_app_from_env(good_env())
    assert app.title and app.docs_url is None and app.openapi_url is None


def test_on_render_a_missing_cache_address_stops_the_start():
    with pytest.raises(SystemExit, match="UPSTASH_REDIS_URL"):
        create_app_from_env(good_env(RENDER="true"))


def test_a_wrong_gateway_setting_stops_the_start_with_a_class_name_only():
    with pytest.raises(SystemExit) as caught:
        create_app_from_env(good_env(GATEWAY_TIMEOUT_SECONDS="soon"))
    assert "gateway settings" in str(caught.value) and "soon" not in str(caught.value)


# ---- how many password hashes may run at once ---------------------------------------------------


def test_the_hashing_limit_defaults_to_two():
    assert make_settings().password_hash_concurrency == 2


@pytest.mark.parametrize("value", [0, -1, 17, True, 1.5, "2"])
def test_a_bad_hashing_limit_is_refused(value):
    with pytest.raises(SettingsError, match="PASSWORD_HASH_CONCURRENCY"):
        make_settings(password_hash_concurrency=value)


def test_the_hashing_limit_is_read_from_the_environment():
    base = {"JWT_SECRET_KEY": JWT_SECRET, "AUDIT_HASH_KEY": AUDIT_KEY}
    four = ApiSettings.from_env({**base, "PASSWORD_HASH_CONCURRENCY": "4"})
    assert four.password_hash_concurrency == 4
    blank = ApiSettings.from_env({**base, "PASSWORD_HASH_CONCURRENCY": " "})
    assert blank.password_hash_concurrency == 2
    with pytest.raises(SettingsError, match="PASSWORD_HASH_CONCURRENCY"):
        ApiSettings.from_env({**base, "PASSWORD_HASH_CONCURRENCY": "many"})


def test_the_hashing_limit_is_documented_as_a_proposal_in_the_env_template():
    from pathlib import Path

    lines = (Path(__file__).resolve().parents[1] / ".env.example").read_text("utf-8").splitlines()
    block = []
    for line in reversed(lines[: lines.index("PASSWORD_HASH_CONCURRENCY=")]):
        if not line.startswith("#"):
            break
        block.append(line)
    comment = " ".join(block)
    assert "[P]" in comment and "Default: 2" in comment and "Where: Render and local" in comment
