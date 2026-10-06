"""Typed errors for the data stores (spec 11.1 store failures).

When Qdrant or Postgres cannot be reached, callers get a StoreUnavailable and show "temporarily
unavailable". When a store answers but refuses our credentials, they get a StoreRejected, which
is a setting to fix and not an outage. Everything else (a wrong query, a missing table, a
deadlock) passes through unchanged so that a bug never hides behind "temporarily unavailable".
Upstash is different: a cache outage is never an error for the caller (see app/cache/store.py).

The errors carry only the name of the underlying exception or an HTTP status, never its text,
because that text can hold a host name, a user name or a URL. `raise ... from None` hides the
original in tracebacks; handlers must still log only the class name, never `str(error)`.
"""

from __future__ import annotations

import socket
import sqlite3
from collections.abc import Generator
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
        self.cause = cause  # the exception class name, or an HTTP status such as "HTTP 503"


class DatabaseUnavailable(StoreUnavailable):
    store = "database"


class VectorStoreUnavailable(StoreUnavailable):
    store = "vector store"


class StoreRejected(Exception):
    """A store was reached but refused our credentials. A setting to fix, not an outage."""

    def __init__(self, store: str, reason: str) -> None:
        super().__init__(f"{store} rejected the request ({reason})")
        self.store = store
        self.reason = reason


def _is_connection_problem(error: psycopg.OperationalError) -> bool:
    """True for a failure to reach or keep the server (not, say, a deadlock or a lock timeout).

    A client-side failure has no SQLSTATE. Server-side, class 08 is "connection exception",
    57P is "operator intervention" (shutdown) and 53 is "insufficient resources".
    """
    state = getattr(error, "sqlstate", None)
    return state is None or state.startswith(("08", "57P", "53"))


def _is_credentials_problem(error: psycopg.OperationalError) -> bool:
    """SQLSTATE class 28 is "invalid authorization". A failed connect can carry no SQLSTATE, so
    the standard server wording is checked too (the text is read here and never copied)."""
    state = getattr(error, "sqlstate", None) or ""
    return state.startswith("28") or "authentication failed" in str(error).lower()


@contextmanager
def database_errors() -> Generator[None]:
    """Map database failures: unreachable -> DatabaseUnavailable, refused credentials ->
    StoreRejected. Anything else (including SQLite errors other than "cannot open the file")
    passes through untouched."""
    outcome: Exception | None = None
    try:
        yield
    except psycopg.OperationalError as error:
        if _is_credentials_problem(error):
            outcome = StoreRejected("database", "credentials")
        elif _is_connection_problem(error):
            outcome = DatabaseUnavailable(type(error).__name__)
        else:
            raise
    except psycopg.InterfaceError as error:  # for example "the connection is closed"
        outcome = DatabaseUnavailable(type(error).__name__)
    except sqlite3.OperationalError as error:
        if "unable to open database" not in str(error).lower():
            raise
        outcome = DatabaseUnavailable(type(error).__name__)
    if outcome is not None:
        raise outcome from None


@contextmanager
def vector_errors() -> Generator[None]:
    """Map Qdrant failures: unreachable, timed out, 429 or 5xx -> VectorStoreUnavailable;
    401 or 403 -> StoreRejected. Other statuses (400, 404, 409 ...) and our own errors pass
    through, so a wrong request or a name clash is never shown as an outage."""
    outcome: Exception | None = None
    try:
        yield
    except qdrant_exceptions.UnexpectedResponse as error:
        status = error.status_code
        if status in (401, 403):
            outcome = StoreRejected("vector store", f"HTTP {status}")
        elif status == 429 or status >= 500:
            outcome = VectorStoreUnavailable(f"HTTP {status}")
        else:
            raise
    except (
        qdrant_exceptions.ResponseHandlingException,
        httpx.TransportError,
        ConnectionError,
        TimeoutError,
        socket.gaierror,
    ) as error:
        outcome = VectorStoreUnavailable(type(error).__name__)
    if outcome is not None:
        raise outcome from None
