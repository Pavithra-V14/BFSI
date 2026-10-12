"""The address a per-address limit counts, behind a proxy (Phase 15; spec 13.1, 13.3).

Rule: with TRUSTED_PROXY_HOPS = 0 (the default) the X-Forwarded-For header is never read, so a
client cannot choose its address. With N hops the entry N places from the end is used, because
each trusted proxy appends what it saw; what a client wrote earlier in the header is never
believed. A header that is too short or holds a non-address falls back to the socket's address.
"""

import pytest
from starlette.requests import Request

from app.api.deps import client_address, forwarded_entries
from app.api.settings import ApiSettings, SettingsError
from tests.api_fixtures import AUDIT_KEY, JWT_SECRET, SOCKET_ADDRESS, Env, make_settings


def request_with(*headers: str, client: tuple[str, int] | None = (SOCKET_ADDRESS, 5000)) -> Request:
    raw = [(b"x-forwarded-for", value.encode()) for value in headers]
    return Request({"type": "http", "headers": raw, "client": client})


# ================================================================================== the settings
def test_the_default_is_zero_and_the_setting_is_read_from_the_environment():
    base = {"JWT_SECRET_KEY": JWT_SECRET, "AUDIT_HASH_KEY": AUDIT_KEY}
    assert ApiSettings.from_env(base).trusted_proxy_hops == 0
    assert ApiSettings.from_env({**base, "TRUSTED_PROXY_HOPS": "1"}).trusted_proxy_hops == 1


@pytest.mark.parametrize("value", ["-1", "4", "one", "1.5"])
def test_a_bad_hop_count_stops_the_start_naming_the_setting(value):
    base = {"JWT_SECRET_KEY": JWT_SECRET, "AUDIT_HASH_KEY": AUDIT_KEY, "TRUSTED_PROXY_HOPS": value}
    with pytest.raises(SettingsError) as caught:
        ApiSettings.from_env(base)
    assert "TRUSTED_PROXY_HOPS" in str(caught.value) and value not in str(caught.value)


# ================================================================================ the address
def test_with_zero_hops_the_header_is_never_read():
    request = request_with("198.51.100.9")
    assert client_address(request, 0) == (SOCKET_ADDRESS, "socket")


def test_with_one_hop_the_last_entry_is_used_and_an_earlier_claim_is_not():
    request = request_with(
        "10.0.0.1, 198.51.100.9"
    )  # the client wrote the first, the proxy the last
    assert client_address(request, 1) == ("198.51.100.9", "forwarded")


def test_with_two_hops_the_second_last_entry_is_used():
    request = request_with("10.0.0.1, 198.51.100.9, 192.0.2.77")
    assert client_address(request, 2) == ("198.51.100.9", "forwarded")


def test_several_header_lines_count_as_one_list_in_order():
    request = request_with("10.0.0.1", "198.51.100.9")
    assert forwarded_entries(request) == ["10.0.0.1", "198.51.100.9"]
    assert client_address(request, 1)[0] == "198.51.100.9"


def test_a_header_shorter_than_the_hops_or_missing_falls_back_to_the_socket():
    assert client_address(request_with("198.51.100.9"), 2) == (SOCKET_ADDRESS, "socket")
    assert client_address(request_with(), 1) == (SOCKET_ADDRESS, "socket")


@pytest.mark.parametrize("entry", ["not-an-address", "999.1.1.1", "198.51.100.9:443", "<script>"])
def test_an_entry_that_is_not_an_address_is_never_used(entry):
    assert client_address(request_with(entry), 1) == (SOCKET_ADDRESS, "socket")


def test_an_ipv6_entry_is_normalised():
    assert client_address(request_with("2001:DB8::1"), 1) == ("2001:db8::1", "forwarded")


def test_a_request_without_a_socket_address_is_unknown():
    assert client_address(request_with(client=None), 0) == ("unknown", "socket")


# ================================================================== through the real limits
def register_many(env, headers_for, count=6):
    codes = []
    for index in range(count):
        body = Env.body(username=f"proxy{index}x")
        codes.append(env.client.post("/auth/register", json=body, headers=headers_for(index)))
    return [reply.status_code for reply in codes]


def test_at_zero_hops_a_changing_header_does_not_get_a_client_more_registrations(tmp_path):
    env = Env(tmp_path, trusted_proxy_hops=0)
    codes = register_many(env, lambda i: {"X-Forwarded-For": f"198.51.100.{i + 1}"})
    assert codes[:5] == [201] * 5 and codes[5] == 429, codes  # the per-address limit is 5


def test_at_one_hop_each_address_the_proxy_saw_is_counted_on_its_own(tmp_path):
    env = Env(tmp_path, trusted_proxy_hops=1)
    codes = register_many(env, lambda i: {"X-Forwarded-For": f"198.51.100.{i + 1}"})
    assert codes == [201] * 6, codes


def test_at_one_hop_what_the_client_wrote_first_cannot_dodge_the_limit(tmp_path):
    env = Env(tmp_path, trusted_proxy_hops=1)
    codes = register_many(env, lambda i: {"X-Forwarded-For": f"10.0.0.{i + 1}, 198.51.100.9"})
    assert codes[:5] == [201] * 5 and codes[5] == 429, codes  # the proxy's entry is the same


# ====================================================================== the diagnostics route
def admin_headers(env):
    env.make_user("boss", role="admin")
    return Env.bearer(env.token("boss"))


def test_the_diagnostics_route_is_for_admins_only(tmp_path):
    env = Env(tmp_path)
    assert env.client.get("/admin/diagnostics").status_code == 401
    env.make_user("asha_rao")
    customer = Env.bearer(env.token("asha_rao"))
    assert env.client.get("/admin/diagnostics", headers=customer).status_code == 403


def test_the_diagnostics_route_shows_the_entries_and_the_address_that_would_be_used(tmp_path):
    env = Env(tmp_path, trusted_proxy_hops=1)
    headers = {**admin_headers(env), "X-Forwarded-For": "10.0.0.1, 198.51.100.9"}
    body = env.client.get("/admin/diagnostics", headers=headers).json()
    assert body["forwarded_entries"] == 2 and body["trusted_proxy_hops"] == 1
    assert body["client_address_used"] == "198.51.100.9" and body["address_source"] == "forwarded"
    assert body["socket_address"] == SOCKET_ADDRESS
    assert set(body) == {
        "forwarded_entries", "trusted_proxy_hops", "client_address_used", "address_source",
        "socket_address", "memory_mb", "peak_memory_mb", "hash_concurrency", "on_render",
        "address_at_one_hop", "source_at_one_hop",
    }  # fmt: skip


def test_at_zero_hops_the_route_still_shows_what_one_hop_would_use(tmp_path):
    """The first deploy ships at 0: the owner compares this with their real public address before
    setting 1 (docs/DEPLOY.md 2.6)."""
    env = Env(tmp_path, trusted_proxy_hops=0)
    admin = admin_headers(env)
    headers = {**admin, "X-Forwarded-For": "10.0.0.1, 198.51.100.9"}
    body = env.client.get("/admin/diagnostics", headers=headers).json()
    assert body["address_at_one_hop"] == "198.51.100.9" and body["source_at_one_hop"] == "forwarded"
    assert body["client_address_used"] == SOCKET_ADDRESS and body["address_source"] == "socket"
    bare = env.client.get("/admin/diagnostics", headers=admin).json()
    assert bare["address_at_one_hop"] == SOCKET_ADDRESS and bare["source_at_one_hop"] == "socket"


def test_at_zero_hops_the_diagnostics_route_says_it_would_use_the_socket_address(tmp_path):
    env = Env(tmp_path, trusted_proxy_hops=0)
    headers = {**admin_headers(env), "X-Forwarded-For": "198.51.100.9"}
    body = env.client.get("/admin/diagnostics", headers=headers).json()
    assert body["client_address_used"] == SOCKET_ADDRESS and body["address_source"] == "socket"
    assert body["forwarded_entries"] == 1


def test_make_settings_accepts_the_new_field():
    assert make_settings(trusted_proxy_hops=2).trusted_proxy_hops == 2
