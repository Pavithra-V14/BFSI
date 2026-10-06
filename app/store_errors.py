"""Typed errors for the data stores (spec 11.1 store failures).

When Qdrant or Postgres cannot be reached, callers get a StoreUnavailable and show "temporarily
unavailable". When a store answers but refuses us (bad credentials, an unknown role or database,
a proxy that does not know the tenant, a missing pg_hba.conf entry, a TLS certificate failure),
they get a StoreRejected: a setting to fix, not an outage. Everything else (a wrong query, a
missing table, a deadlock, a version mismatch) passes through unchanged so that a bug never hides
behind "temporarily unavailable". Upstash is different: a cache outage is never an error for the
caller (see app/cache/store.py).

The errors carry only the name of the underlying exception, an HTTP status or a fixed reason,
never its text, because that text can hold a host name, a user name or a URL.

`call_database` and `call_vector` run a function and translate its failure. The new error is
raised after the `except` block has ended, so it has no `__context__` and no `__cause__`: the
original exception, and the host text it carries, is not reachable from the new one. (A
`with` block cannot do this: an error raised from `__exit__` is always chained to the original.)
"""

from __future__ import annotations

import re
import socket
import sqlite3
import ssl
from collections.abc import Callable
from typing import TypeVar

import httpx
import psycopg
from qdrant_client.http import exceptions as qdrant_exceptions

USER_MESSAGE = "temporarily unavailable"

T = TypeVar("T")


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
    """A store was reached but refused us. A setting to fix, not an outage.

    reason is "credentials" (the key or password was refused) or "settings" (an unknown role or
    database, an unknown tenant, no pg_hba.conf entry, or a TLS certificate failure).
    """

    def __init__(self, store: str, reason: str) -> None:
        super().__init__(f"{store} rejected the request ({reason})")
        self.store = store
        self.reason = reason


# ---- Postgres ---------------------------------------------------------------------------

# A failed connect has no SQLSTATE (there is no server result), so the standard libpq and
# server wording is recognised too. The text is read here and never copied anywhere.
_CREDENTIAL_WORDS = ("authentication failed",)
_SETTINGS_WORDS = (
    "tenant or user not found",  # a Supabase pooler that does not know the project or user
    "no pg_hba.conf entry",
    "certificate verify failed",
    "server certificate for",
    "server does not support ssl",
    "ssl error: tlsv1 alert unknown ca",
)
_UNKNOWN_ROLE_OR_DATABASE = re.compile(r'(role|database) ".*" does not exist', re.IGNORECASE)


def _is_connection_problem(error: psycopg.OperationalError) -> bool:
    """True for a failure to reach or keep the server (not, say, a deadlock or a lock timeout).

    A client-side failure has no SQLSTATE. Server-side, class 08 is "connection exception",
    57P is "operator intervention" (shutdown) and 53 is "insufficient resources".
    """
    state = getattr(error, "sqlstate", None)
    return state is None or state.startswith(("08", "57P", "53"))


def _postgres_rejection(error: psycopg.OperationalError) -> str | None:
    """ "credentials", "settings" or None, from the SQLSTATE and the standard wording."""
    state = getattr(error, "sqlstate", None) or ""
    text = str(error).lower()
    if state.startswith("28") or any(word in text for word in _CREDENTIAL_WORDS):
        return "credentials"
    if (
        state == "3D000"  # invalid catalog name: the database does not exist
        or any(word in text for word in _SETTINGS_WORDS)
        or _UNKNOWN_ROLE_OR_DATABASE.search(text)
    ):
        return "settings"
    return None


def call_database(work: Callable[[], T]) -> T:
    """Run `work`. Unreachable database -> DatabaseUnavailable; refused credentials or settings ->
    StoreRejected. Anything else, including SQLite errors other than "cannot open the file",
    passes through untouched."""
    outcome: Exception | None = None
    try:
        return work()
    except (psycopg.OperationalError, psycopg.InterfaceError, sqlite3.OperationalError) as error:
        outcome = classify_database_error(error)
        if outcome is None:
            raise
    raise outcome  # outside the except block: no __context__, nothing of the original


def classify_database_error(error: BaseException) -> Exception | None:
    """The typed error for a database failure, or None if it must pass through unchanged.

    The API's exception handlers use this too, so a failure that surfaces outside call_database
    is classified by the same rules.
    """
    if isinstance(error, psycopg.OperationalError):
        rejection = _postgres_rejection(error)
        if rejection is not None:
            return StoreRejected("database", rejection)
        if _is_connection_problem(error):
            return DatabaseUnavailable(type(error).__name__)
        return None
    if isinstance(error, psycopg.InterfaceError):  # for example "the connection is closed"
        return DatabaseUnavailable(type(error).__name__)
    if isinstance(error, sqlite3.OperationalError):
        if "unable to open database" in str(error).lower():
            return DatabaseUnavailable(type(error).__name__)
    return None


# ---- Qdrant -----------------------------------------------------------------------------


def _chain(error: BaseException | None) -> list[BaseException]:
    seen: list[BaseException] = []
    while error is not None and error not in seen and len(seen) < 8:
        seen.append(error)
        error = error.__cause__ or error.__context__
    return seen


def _qdrant_outcome(error: BaseException) -> Exception | None:
    """The typed error for a Qdrant failure, or None if it must pass through unchanged.

    Only transport-level failures (cannot connect, timed out, connection dropped, DNS) are an
    outage. A bad certificate is a setting. Everything else the REST layer wraps in
    ResponseHandlingException (bad JSON, a response of the wrong shape, a bad URL or scheme, a
    client and server that do not match) is a bug or a misconfiguration and is not hidden.
    """
    if isinstance(error, qdrant_exceptions.UnexpectedResponse):
        status = error.status_code
        if status in (401, 403):
            return StoreRejected("vector store", f"HTTP {status}")
        if status == 429 or status >= 500:
            return VectorStoreUnavailable(f"HTTP {status}")
        return None
    source: BaseException = error
    if isinstance(error, qdrant_exceptions.ResponseHandlingException):
        source = getattr(error, "source", None) or error
    if any(isinstance(item, ssl.SSLCertVerificationError) for item in _chain(source)):
        return StoreRejected("vector store", "settings")
    if isinstance(source, httpx.UnsupportedProtocol):  # a wrong scheme: a setting, not an outage
        return None
    if isinstance(source, httpx.TransportError | ConnectionError | TimeoutError | socket.gaierror):
        return VectorStoreUnavailable(type(source).__name__)
    return None


def call_vector(work: Callable[[], T]) -> T:
    """Run `work`. A Qdrant that cannot be reached, times out, answers 429 or 5xx ->
    VectorStoreUnavailable; 401 or 403, or a bad certificate -> StoreRejected. Other statuses
    (400, 404, 409 ...), wrapped non-transport failures and our own errors pass through."""
    outcome: Exception | None = None
    try:
        return work()
    except (
        qdrant_exceptions.ApiException,
        httpx.HTTPError,
        ConnectionError,
        TimeoutError,
        socket.gaierror,
    ) as error:
        outcome = _qdrant_outcome(error)
        if outcome is None:
            raise
    raise outcome  # outside the except block: no __context__, nothing of the original
