"""Store failures become typed errors (spec 11.1): "temporarily unavailable" for a store that
cannot be reached, a settings error for one that refuses us, and nothing of the original text."""

import json
import socket
import sqlite3
import ssl

import httpx
import psycopg
import pytest
from qdrant_client.http import exceptions as qdrant_exceptions

from app.store_errors import (
    DatabaseUnavailable,
    StoreRejected,
    StoreUnavailable,
    VectorStoreUnavailable,
    call_database,
    call_vector,
)

HOST = "ep-quiet-sky.example.test"
ADDRESS = "203.0.113.9"
USER = "asha_admin"


class Sqlstate(psycopg.OperationalError):
    def __init__(self, text, state):
        self._state = state  # set first: psycopg's own __init__ reads sqlstate
        super().__init__(text)

    @property
    def sqlstate(self):
        return self._state


def failing(error):
    def work():
        raise error

    return work


def raised_by(call, error):
    """The exception that comes out of `call(work)` when `work` raises `error`."""
    with pytest.raises(BaseException) as caught:
        call(failing(error))
    return caught.value


def outcome_of(call, work):
    """The exception that comes out of `call(work)` for a real failing function."""
    with pytest.raises(BaseException) as caught:
        call(work)
    return caught.value


def libpq(message):
    """The shape of psycopg's text for a failed connect, with a host, an address and a user."""
    return (
        f'connection failed: connection to server at "{HOST}" ({ADDRESS}), port 5432 failed: '
        f"{message}"
    )


def assert_nothing_leaks(error):
    text = f"{error} {error!r} {getattr(error, 'cause', '')} {getattr(error, 'reason', '')}"
    for secret in (HOST, ADDRESS, USER, "5432"):
        assert secret not in text
    assert error.__context__ is None  # the original exception is not reachable from this one
    assert error.__cause__ is None


# ---- Postgres: unreachable ---------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Connection refused\n\tIs the server running on that host and accepting TCP/IP "
        "connections?",
        "timeout expired",
        f'could not translate host name "{HOST}" to address: Name or service not known',
        "server closed the connection unexpectedly",
    ],
)
def test_a_database_that_cannot_be_reached_is_database_unavailable(message):
    error = raised_by(call_database, psycopg.OperationalError(libpq(message)))
    assert isinstance(error, DatabaseUnavailable) and isinstance(error, StoreUnavailable)
    assert str(error) == "database is temporarily unavailable"
    assert error.cause == "OperationalError"
    assert_nothing_leaks(error)


def test_a_real_connect_to_a_closed_local_port_stays_unavailable_and_leaks_nothing():
    def connect():
        # Nothing listens on port 1 of this machine: a real failure with no SQLSTATE.
        return psycopg.connect(f"postgresql://{USER}:pw@127.0.0.1:1/db", connect_timeout=2)

    error = outcome_of(call_database, connect)
    assert isinstance(error, DatabaseUnavailable) and not isinstance(error, StoreRejected)
    assert str(error) == "database is temporarily unavailable"
    assert_nothing_leaks(error)


@pytest.mark.parametrize("state", ["08006", "57P01", "53300"])
def test_server_side_connection_failures_count_as_unavailable(state):
    error = raised_by(call_database, Sqlstate(libpq("server went away"), state))
    assert isinstance(error, DatabaseUnavailable)
    assert_nothing_leaks(error)


def test_a_closed_connection_is_unavailable():
    error = raised_by(call_database, psycopg.InterfaceError("connection is closed"))
    assert isinstance(error, DatabaseUnavailable)
    assert error.__context__ is None


# ---- Postgres: refused (a settings error, not an outage) -----------------------------------

REFUSED = {
    "credentials": [
        libpq(f'FATAL:  password authentication failed for user "{USER}"'),
        libpq(f'FATAL:  password authentication failed for user "{USER}"\nconnection ...'),
    ],
    "settings": [
        libpq(f'FATAL:  role "{USER}" does not exist'),
        libpq('FATAL:  database "appdb" does not exist'),
        libpq("FATAL:  Tenant or user not found"),
        libpq(
            f'FATAL:  no pg_hba.conf entry for host "{ADDRESS}", user "{USER}", '
            'database "appdb", SSL encryption'
        ),
        libpq(f'FATAL:  no pg_hba.conf entry for host "{ADDRESS}", user "{USER}", no encryption'),
        libpq("SSL error: certificate verify failed"),
        libpq('server certificate for "other.example.test" does not match host name "x"'),
        libpq("server does not support SSL, but SSL was required"),
    ],
}


@pytest.mark.parametrize(
    ("reason", "message"),
    [(reason, text) for reason, texts in REFUSED.items() for text in texts],
)
def test_a_refusal_is_a_settings_error_not_an_outage_and_shows_nothing(reason, message):
    error = raised_by(call_database, psycopg.OperationalError(message))
    assert isinstance(error, StoreRejected) and not isinstance(error, StoreUnavailable)
    assert error.reason == reason and error.store == "database"
    assert str(error) == f"database rejected the request ({reason})"
    assert_nothing_leaks(error)


@pytest.mark.parametrize("state", ["28P01", "28000"])
def test_class_28_sqlstates_are_refused_credentials(state):
    error = raised_by(call_database, Sqlstate(libpq("FATAL: anything"), state))
    assert isinstance(error, StoreRejected) and error.reason == "credentials"


def test_sqlstate_3d000_is_an_unknown_database():
    error = raised_by(call_database, Sqlstate(libpq("FATAL: x"), "3D000"))
    assert isinstance(error, StoreRejected) and error.reason == "settings"


@pytest.mark.parametrize("state", ["40P01", "55P03", "42P01"])
def test_a_deadlock_or_a_bad_query_is_not_an_outage_or_a_refusal(state):
    error = raised_by(call_database, Sqlstate("not an outage", state))
    assert isinstance(error, Sqlstate)  # passes through untouched


def test_other_database_exceptions_pass_through_untouched():
    assert isinstance(raised_by(call_database, ValueError("a bug")), ValueError)
    duplicate = raised_by(call_database, sqlite3.IntegrityError("duplicate"))
    assert isinstance(duplicate, sqlite3.IntegrityError)


def test_sqlite_only_counts_a_file_that_cannot_be_opened_as_unavailable():
    error = outcome_of(call_database, lambda: sqlite3.connect("/no/such/folder/x/db.sqlite"))
    assert isinstance(error, DatabaseUnavailable)
    # A missing table or a lock is a bug or a conflict, not an outage.
    for message in ("no such table: users", "database is locked", 'near "SELEC": syntax error'):
        passed = raised_by(call_database, sqlite3.OperationalError(message))
        assert isinstance(passed, sqlite3.OperationalError)


def test_a_call_that_succeeds_returns_its_value():
    assert call_database(lambda: 42) == 42
    assert call_vector(lambda: "ok") == "ok"


# ---- Qdrant ----------------------------------------------------------------------------


def cert_failure():
    """A connect error whose cause is a certificate failure, as httpx raises it."""
    outer = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    outer.__cause__ = ssl.SSLCertVerificationError(1, f"certificate verify failed for {HOST}")
    return outer


@pytest.mark.parametrize(
    "error",
    [
        qdrant_exceptions.UnexpectedResponse(503, "Service Unavailable", b"", None),
        qdrant_exceptions.UnexpectedResponse(500, "Server Error", b"", None),
        qdrant_exceptions.UnexpectedResponse(429, "Too Many Requests", b"", None),
        # the REST layer wraps transport failures in ResponseHandlingException
        qdrant_exceptions.ResponseHandlingException(httpx.ConnectError(f"cannot connect {HOST}")),
        qdrant_exceptions.ResponseHandlingException(httpx.ReadTimeout("slow")),
        qdrant_exceptions.ResponseHandlingException(httpx.RemoteProtocolError("dropped")),
        qdrant_exceptions.ResponseHandlingException(ConnectionRefusedError(f"refused {HOST}")),
        qdrant_exceptions.ResponseHandlingException(socket.gaierror(f"no such host {HOST}")),
        httpx.ConnectError(f"cannot connect to {HOST}"),
        httpx.ReadTimeout("slow"),
        TimeoutError("slow"),
        ConnectionRefusedError(f"refused by {HOST}"),
        socket.gaierror(f"name resolution failed for {HOST}"),
    ],
)
def test_a_qdrant_that_cannot_be_reached_is_vector_store_unavailable(error):
    caught = raised_by(call_vector, error)
    assert isinstance(caught, VectorStoreUnavailable)
    assert str(caught) == "vector store is temporarily unavailable"
    assert caught.cause
    assert_nothing_leaks(caught)


@pytest.mark.parametrize("status", [401, 403])
def test_refused_qdrant_credentials_are_a_rejection_not_an_outage(status):
    error = raised_by(call_vector, qdrant_exceptions.UnexpectedResponse(status, "x", b"", None))
    assert isinstance(error, StoreRejected) and not isinstance(error, StoreUnavailable)
    assert str(error) == f"vector store rejected the request (HTTP {status})"
    assert error.store == "vector store" and error.reason == f"HTTP {status}"
    assert error.__context__ is None


@pytest.mark.parametrize(
    "error",
    [cert_failure(), qdrant_exceptions.ResponseHandlingException(cert_failure())],
)
def test_a_bad_certificate_is_a_settings_error_not_an_outage(error):
    caught = raised_by(call_vector, error)
    assert isinstance(caught, StoreRejected) and caught.reason == "settings"
    assert_nothing_leaks(caught)


@pytest.mark.parametrize("status", [400, 404, 409, 422])
def test_a_wrong_request_or_a_name_clash_is_not_shown_as_an_outage(status):
    error = raised_by(call_vector, qdrant_exceptions.UnexpectedResponse(status, "x", b"", None))
    assert isinstance(error, qdrant_exceptions.UnexpectedResponse)
    assert error.status_code == status


@pytest.mark.parametrize(
    "source",
    [
        json.JSONDecodeError("bad json", "{", 1),  # the server sent something that is not JSON
        ValueError("response does not match the expected model"),  # a client/server mismatch
        httpx.UnsupportedProtocol("Request URL has an unsupported protocol 'ftp://'"),
        KeyError("points"),
    ],
)
def test_wrapped_failures_that_are_not_transport_problems_pass_through(source):
    error = qdrant_exceptions.ResponseHandlingException(source)
    assert raised_by(call_vector, error) is error  # unchanged: a bug is not an outage


@pytest.mark.parametrize("error", [PermissionError("denied"), FileNotFoundError("missing")])
def test_local_file_problems_are_not_a_vector_outage(error):
    assert raised_by(call_vector, error) is error


def test_our_own_errors_are_not_turned_into_outages():
    from app.vector.documents import CollectionMismatch

    assert isinstance(raised_by(call_vector, CollectionMismatch("wrong")), CollectionMismatch)
    assert isinstance(raised_by(call_vector, KeyError("a bug")), KeyError)


def test_the_new_error_has_no_context_even_when_called_inside_an_except_block():
    # Called from inside a handler, the new error must still not drag the original along.
    try:
        raise RuntimeError("the caller's own problem")
    except RuntimeError:
        error = raised_by(call_database, psycopg.OperationalError(libpq("timeout expired")))
    assert isinstance(error, DatabaseUnavailable)
    assert error.__cause__ is None
    assert "timeout expired" not in repr(error.__context__ or "")
