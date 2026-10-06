"""Typed errors for the data stores (spec 11.1 store failures).

When Qdrant or Postgres cannot be reached, callers get one of these and show "temporarily
unavailable". Upstash is different: a cache outage is never an error for the caller (see
app/cache/store.py), so it has no exception here. The errors carry only the name of the
underlying exception, never its text, because that text can hold a host name or a URL.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import psycopg
from qdrant_client.http import exceptions as qdrant_exceptions

USER_MESSAGE = "temporarily unavailable"


class StoreUnavailable(Exception):
    """A data store could not be reached. Show the user "temporarily unavailable"."""

    store = "store"

    def __init__(self, cause: str | None = None) -> None:
        super().__init__(f"{self.store} is {USER_MESSAGE}")
        self.cause = cause  # the exception class name only


class DatabaseUnavailable(StoreUnavailable):
    store = "database"


class VectorStoreUnavailable(StoreUnavailable):
    store = "vector store"


def _is_connection_problem(error: psycopg.OperationalError) -> bool:
    """True for a failure to reach or keep the server (not, say, a deadlock or a lock timeout).

    A client-side failure has no SQLSTATE. Server-side, class 08 is "connection exception",
    57P is "operator intervention" (shutdown) and 53 is "insufficient resources".
    """
    state = getattr(error, "sqlstate", None)
    return state is None or state.startswith(("08", "57P", "53"))


@contextmanager
def database_errors() -> Iterator[None]:
    """Turn "cannot reach the database" into DatabaseUnavailable. Other errors pass through."""
    try:
        yield
    except psycopg.OperationalError as error:
        if not _is_connection_problem(error):
            raise
        raise DatabaseUnavailable(type(error).__name__) from None
    except (psycopg.InterfaceError, sqlite3.OperationalError) as error:
        raise DatabaseUnavailable(type(error).__name__) from None


@contextmanager
def vector_errors() -> Iterator[None]:
    """Turn "cannot reach Qdrant" into VectorStoreUnavailable. Our own errors pass through."""
    try:
        yield
    except (qdrant_exceptions.ApiException, httpx.HTTPError, OSError) as error:
        raise VectorStoreUnavailable(type(error).__name__) from None
