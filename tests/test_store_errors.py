"""Store failures become typed errors (spec 11.1): "temporarily unavailable", no address shown."""

import socket
import sqlite3

import httpx
import psycopg
import pytest
from qdrant_client.http import exceptions as qdrant_exceptions

from app.store_errors import (
    DatabaseUnavailable,
    StoreRejected,
    StoreUnavailable,
    VectorStoreUnavailable,
    database_errors,
    vector_errors,
)

SECRET = "db-host-secret.example.test"


class Sqlstate(psycopg.OperationalError):
    def __init__(self, text, state):
        self._state = state  # set first: psycopg's own __init__ reads sqlstate
        super().__init__(text)

    @property
    def sqlstate(self):
        return self._state


def raised_by(manager, error):
    """The exception that comes out of `with manager(): raise error`."""
    with pytest.raises(BaseException) as caught:
        with manager():
            raise error
    return caught.value


# ---- Postgres --------------------------------------------------------------------------


def test_a_database_that_cannot_be_reached_is_database_unavailable():
    error = raised_by(
        database_errors, psycopg.OperationalError(f"could not connect to server at {SECRET}")
    )
    assert isinstance(error, DatabaseUnavailable) and isinstance(error, StoreUnavailable)
    assert str(error) == "database is temporarily unavailable"
    assert error.cause == "OperationalError" and SECRET not in str(error)
    assert error.__suppress_context__  # the original is hidden from tracebacks


@pytest.mark.parametrize("state", ["08006", "57P01", "53300"])
def test_server_side_connection_failures_count_as_unavailable(state):
    assert isinstance(
        raised_by(database_errors, Sqlstate("server went away", state)), DatabaseUnavailable
    )


@pytest.mark.parametrize("state", ["40P01", "55P03", "42P01"])
def test_a_deadlock_or_a_bad_query_is_not_an_outage(state):
    error = raised_by(database_errors, Sqlstate("not an outage", state))
    assert isinstance(error, Sqlstate)  # passes through untouched


@pytest.mark.parametrize(
    "error",
    [
        Sqlstate(f"FATAL: password authentication failed for user 'u' at {SECRET}", "28P01"),
        Sqlstate(f"FATAL: role does not exist ({SECRET})", "28000"),
        # A failed connect can carry no SQLSTATE: the standard wording is recognised instead.
        psycopg.OperationalError(f'connection to "{SECRET}" failed: authentication failed'),
    ],
)
def test_refused_credentials_are_a_rejection_not_an_outage_and_show_nothing(error):
    rejected = raised_by(database_errors, error)
    assert isinstance(rejected, StoreRejected) and not isinstance(rejected, StoreUnavailable)
    assert str(rejected) == "database rejected the request (credentials)"
    assert SECRET not in str(rejected) and "'u'" not in str(rejected)


def test_other_database_exceptions_pass_through_untouched():
    assert isinstance(raised_by(database_errors, ValueError("a bug")), ValueError)
    assert isinstance(
        raised_by(database_errors, sqlite3.IntegrityError("duplicate")), sqlite3.IntegrityError
    )


def test_a_closed_connection_is_unavailable():
    error = raised_by(database_errors, psycopg.InterfaceError("connection is closed"))
    assert isinstance(error, DatabaseUnavailable)


def test_sqlite_only_counts_a_file_that_cannot_be_opened_as_unavailable():
    with pytest.raises(DatabaseUnavailable):
        with database_errors():
            sqlite3.connect("/no/such/folder/at/all/db.sqlite")
    # A missing table or a lock is a bug or a conflict, not an outage.
    for message in ("no such table: users", "database is locked", 'near "SELEC": syntax error'):
        assert isinstance(
            raised_by(database_errors, sqlite3.OperationalError(message)), sqlite3.OperationalError
        )


# ---- Qdrant ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        qdrant_exceptions.ResponseHandlingException(Exception(f"connect to {SECRET} failed")),
        qdrant_exceptions.UnexpectedResponse(503, "Service Unavailable", b"", None),
        qdrant_exceptions.UnexpectedResponse(500, "Server Error", b"", None),
        qdrant_exceptions.UnexpectedResponse(429, "Too Many Requests", b"", None),
        httpx.ConnectError(f"cannot connect to {SECRET}"),
        httpx.ReadTimeout("slow"),
        TimeoutError("slow"),
        ConnectionRefusedError(f"refused by {SECRET}"),
        socket.gaierror(f"name resolution failed for {SECRET}"),
    ],
)
def test_a_qdrant_that_cannot_be_reached_is_vector_store_unavailable(error):
    caught = raised_by(vector_errors, error)
    assert isinstance(caught, VectorStoreUnavailable)
    assert str(caught) == "vector store is temporarily unavailable"
    assert SECRET not in str(caught) and caught.cause


@pytest.mark.parametrize("status", [401, 403])
def test_refused_qdrant_credentials_are_a_rejection_not_an_outage(status):
    error = raised_by(vector_errors, qdrant_exceptions.UnexpectedResponse(status, "x", b"", None))
    assert isinstance(error, StoreRejected) and not isinstance(error, StoreUnavailable)
    assert str(error) == f"vector store rejected the request (HTTP {status})"
    assert error.store == "vector store" and error.reason == f"HTTP {status}"


@pytest.mark.parametrize("status", [400, 404, 409, 422])
def test_a_wrong_request_or_a_name_clash_is_not_shown_as_an_outage(status):
    error = raised_by(vector_errors, qdrant_exceptions.UnexpectedResponse(status, "x", b"", None))
    assert isinstance(error, qdrant_exceptions.UnexpectedResponse)
    assert error.status_code == status


@pytest.mark.parametrize("error", [PermissionError("denied"), FileNotFoundError("missing")])
def test_local_file_problems_are_not_a_vector_outage(error):
    assert raised_by(vector_errors, error) is error


def test_our_own_errors_are_not_turned_into_outages():
    from app.vector.documents import CollectionMismatch

    assert isinstance(
        raised_by(vector_errors, CollectionMismatch("wrong shape")), CollectionMismatch
    )
    assert isinstance(raised_by(vector_errors, KeyError("a bug")), KeyError)


def test_nothing_is_raised_when_nothing_goes_wrong():
    with vector_errors():
        pass
    with database_errors():
        pass
