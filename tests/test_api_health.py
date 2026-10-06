"""Liveness (public, reveals nothing) and health (admin only), spec 11.2."""

import pytest
from qdrant_client import QdrantClient

from app.vector.documents import ensure_documents_collection
from tests.api_fixtures import FakeGateway
from tests.test_cache_store import FakeClock, FlakyRedis


def test_live_is_public_and_says_two_words(env):
    response = env.client.get("/live")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_live_is_the_same_for_everyone(env):
    token = env.admin_token()
    anonymous = env.client.get("/live").content
    with_token = env.client.get("/live", headers=env.bearer(token)).content
    with_junk = env.client.get("/live", headers={"Authorization": "Bearer junk"}).content
    assert anonymous == with_token == with_junk


def test_live_touches_no_database_no_cache_and_no_model(env):
    def forbidden():
        raise AssertionError("/live must not open a database")

    env.app.state.services.db_factory = forbidden
    env.app.state.services.cache = None
    env.app.state.services.gateway = None
    assert env.client.get("/live").status_code == 200


def test_live_works_even_when_everything_else_is_down(env):
    env.app.state.services.db_factory = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    assert env.client.get("/live").json() == {"status": "ok"}


def test_live_reveals_no_version_name_or_setting(env):
    text = env.client.get("/live").text.lower()
    for word in ("version", "fastapi", "uvicorn", "database", "qdrant", "upstash", "key", "user"):
        assert word not in text


def test_live_has_no_other_methods(env):
    assert env.client.post("/live").status_code == 405


# ---- /health ----------------------------------------------------------------------------------


def test_health_needs_a_token(env):
    assert env.client.get("/health").status_code == 401


@pytest.mark.parametrize("role", ["customer", "staff"])
def test_health_is_refused_to_everyone_but_an_admin(env, role):
    env.make_user("ravi_k", role=role)
    response = env.client.get("/health", headers=env.bearer(env.token("ravi_k")))
    assert response.status_code == 403


def test_an_admin_sees_the_health_of_each_dependency(env):
    token = env.admin_token()
    body = env.client.get("/health", headers=env.bearer(token)).json()
    assert body["status"] == "ok"
    assert body["checks"] == {"database": "ok", "cache": "ok", "vector_store": "not_configured"}
    assert body["providers"] == {}


def test_health_reports_a_dead_cache_as_degraded(env):
    token = env.admin_token()
    env.app.state.services.cache = type(env.cache)(FlakyRedis(), clock=FakeClock())
    body = env.client.get("/health", headers=env.bearer(token)).json()
    assert body["checks"]["cache"] == "unavailable" and body["status"] == "degraded"


def test_health_with_no_cache_configured_says_so_and_is_still_ok(env):
    token = env.admin_token()
    env.app.state.services.cache = None
    body = env.client.get("/health", headers=env.bearer(token)).json()
    assert body["checks"]["cache"] == "not_configured" and body["status"] == "ok"


def test_health_reports_the_vector_store(env):
    token = env.admin_token()
    client = QdrantClient(":memory:")
    env.app.state.services.vector_client = client
    missing = env.client.get("/health", headers=env.bearer(token)).json()
    assert missing["checks"]["vector_store"] == "missing_collection"
    assert missing["status"] == "degraded"
    ensure_documents_collection(client)
    ready = env.client.get("/health", headers=env.bearer(token)).json()
    assert ready["checks"]["vector_store"] == "ok" and ready["status"] == "ok"


def test_health_reports_an_unreachable_vector_store_without_its_address(env):
    token = env.admin_token()
    env.app.state.services.vector_client = QdrantClient(
        url="http://127.0.0.1:1", timeout=1, check_compatibility=False
    )
    response = env.client.get("/health", headers=env.bearer(token))
    assert response.json()["checks"]["vector_store"] == "unavailable"
    assert "127.0.0.1" not in response.text


def test_health_reports_a_rejected_vector_store_key_as_rejected(env):
    from qdrant_client.http import exceptions as qdrant_exceptions

    class Rejecting:
        def collection_exists(self, name):
            raise qdrant_exceptions.UnexpectedResponse(401, "Unauthorized", b"", None)

    token = env.admin_token()
    env.app.state.services.vector_client = Rejecting()
    body = env.client.get("/health", headers=env.bearer(token)).json()
    assert body["checks"]["vector_store"] == "rejected"


def test_health_shows_only_the_state_and_key_count_of_each_provider(env):
    token = env.admin_token()
    env.app.state.services.gateway = FakeGateway(
        {
            "groq": {"keys": 2, "state": "healthy", "tiers": {"answer": "closed"}},
            "gemini": {"keys": 1, "state": "circuit_open", "tiers": {"answer": "open"}},
        }
    )
    body = env.client.get("/health", headers=env.bearer(token)).json()
    assert body["providers"] == {
        "groq": {"state": "healthy", "keys": 2},
        "gemini": {"state": "circuit_open", "keys": 1},
    }
    assert body["status"] == "degraded"  # one provider is not healthy


def test_health_never_contains_a_key_an_address_or_an_error_text(env):
    from tests.api_fixtures import AUDIT_KEY, JWT_SECRET

    token = env.admin_token()
    text = env.client.get("/health", headers=env.bearer(token)).text
    for forbidden in (JWT_SECRET, AUDIT_KEY, env.url, str(env.path), "sqlite", "Traceback"):
        assert forbidden not in text


def test_a_database_that_is_down_makes_health_a_503_because_the_token_check_needs_it(env):
    from app.store_errors import DatabaseUnavailable

    token = env.admin_token()

    def down():
        raise DatabaseUnavailable("OperationalError")

    env.app.state.services.db_factory = down
    response = env.client.get("/health", headers=env.bearer(token))
    assert response.status_code == 503 and "OperationalError" not in response.text


def test_the_database_check_itself_reports_unavailable_and_rejected():
    from app.api.routes.health import _database
    from app.store_errors import DatabaseUnavailable, StoreRejected

    class Down:
        def query_one(self, sql):
            raise DatabaseUnavailable("OperationalError")

    class Refused:
        def query_one(self, sql):
            raise StoreRejected("database", "credentials")

    assert _database(Down()) == "unavailable"
    assert _database(Refused()) == "rejected"


def test_health_counts_against_the_per_user_rate_limit(env):
    token = env.admin_token()
    codes = [env.client.get("/health", headers=env.bearer(token)).status_code for _ in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429
