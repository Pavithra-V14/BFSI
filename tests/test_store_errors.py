"""Store failures become typed errors (spec 11.1): "temporarily unavailable", no address shown."""

import sqlite3

import httpx
import psycopg
import pytest
from qdrant_client.http import exceptions as qdrant_exceptions

from app.store_errors import (
    DatabaseUnavailable,
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


def test_a_database_that_cannot_be_reached_is_database_unavailable():
    with pytest.raises(DatabaseUnavailable) as caught:
        with database_errors():
            raise psycopg.OperationalError(f"could not connect to server at {SECRET}")
    assert str(caught.value) == "database is temporarily unavailable"
    assert caught.value.cause == "OperationalError" and SECRET not in str(caught.value)
    assert isinstance(caught.value, StoreUnavailable)


@pytest.mark.parametrize("state", ["08006", "57P01", "53300"])
def test_server_side_connection_failures_count_as_unavailable(state):
    with pytest.raises(DatabaseUnavailable):
        with database_errors():
            raise Sqlstate("server went away", state)


@pytest.mark.parametrize("state", ["40P01", "55P03", "42P01"])
def test_a_deadlock_or_a_bad_query_is_not_an_outage(state):
    with pytest.raises(Sqlstate):
        with database_errors():
            raise Sqlstate("not an outage", state)


def test_other_database_exceptions_pass_through_untouched():
    with pytest.raises(ValueError):
        with database_errors():
            raise ValueError("a bug")
    with pytest.raises(sqlite3.IntegrityError):
        with database_errors():
            raise sqlite3.IntegrityError("duplicate")


def test_a_closed_connection_and_an_unopenable_sqlite_file_are_unavailable():
    with pytest.raises(DatabaseUnavailable):
        with database_errors():
            raise psycopg.InterfaceError("connection is closed")
    with pytest.raises(DatabaseUnavailable):
        with database_errors():
            sqlite3.connect("/no/such/folder/at/all/db.sqlite")


@pytest.mark.parametrize(
    "error",
    [
        qdrant_exceptions.ResponseHandlingException(Exception(f"connect to {SECRET} failed")),
        qdrant_exceptions.UnexpectedResponse(503, "Service Unavailable", b"", None),
        httpx.ConnectError(f"cannot connect to {SECRET}"),
        httpx.ReadTimeout("slow"),
        TimeoutError("slow"),
        ConnectionRefusedError(f"refused by {SECRET}"),
    ],
)
def test_a_qdrant_that_cannot_be_reached_is_vector_store_unavailable(error):
    with pytest.raises(VectorStoreUnavailable) as caught:
        with vector_errors():
            raise error
    assert str(caught.value) == "vector store is temporarily unavailable"
    assert SECRET not in str(caught.value) and caught.value.cause == type(error).__name__


def test_our_own_errors_are_not_turned_into_outages():
    from app.vector.documents import CollectionMismatch

    with pytest.raises(CollectionMismatch):
        with vector_errors():
            raise CollectionMismatch("wrong shape")
    with pytest.raises(KeyError):
        with vector_errors():
            raise KeyError("a bug")
