"""One error shape for the whole API: {"error": {"code": ..., "message": ..., ...}}.

Messages are plain words for the user. Nothing the user sent is ever echoed back (FastAPI's
default 422 repeats the input, which would include a password), and nothing from a store or a
library: no host, no address, no SQL, no stack trace.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.store_errors import (
    StoreRejected,
    StoreUnavailable,
    classify_database_error,
)

log = logging.getLogger("app.api")

UNAVAILABLE_MESSAGE = "The service is temporarily unavailable. Please try again shortly."


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}
        self.extra = extra or {}


def error_response(
    status: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body = {"error": {"code": code, "message": message, **(extra or {})}}
    return JSONResponse(body, status_code=status, headers=headers)


def _store_response(error: Exception) -> JSONResponse:
    if isinstance(error, StoreRejected):
        # A setting to fix: the operator needs to know, the user only sees "unavailable".
        log.error("store rejected the request: store=%s reason=%s", error.store, error.reason)
    else:
        log.error("store unavailable: %s cause=%s", type(error).__name__, error.cause)  # type: ignore[attr-defined]
    return error_response(503, "service_unavailable", UNAVAILABLE_MESSAGE)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return error_response(
            error.status, error.code, error.message, headers=error.headers, extra=error.extra
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # Only where the problem is, never what was sent.
        fields = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors()})
        return error_response(
            422,
            "invalid_request",
            "The request is not valid.",
            extra={"fields": fields},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed"}
        messages = {404: "Not found.", 405: "That method is not allowed here."}
        return error_response(
            error.status_code,
            codes.get(error.status_code, "error"),
            messages.get(error.status_code, "The request could not be completed."),
        )

    @app.exception_handler(StoreUnavailable)
    async def store_unavailable(request: Request, error: StoreUnavailable) -> JSONResponse:
        return _store_response(error)

    @app.exception_handler(StoreRejected)
    async def store_rejected(request: Request, error: StoreRejected) -> JSONResponse:
        return _store_response(error)

    async def database_failure(request: Request, error: Exception) -> JSONResponse:
        typed = classify_database_error(error)
        if typed is None:
            return _server_error(error)
        return _store_response(typed)

    for kind in (psycopg.OperationalError, psycopg.InterfaceError, sqlite3.OperationalError):
        app.add_exception_handler(kind, database_failure)

    @app.exception_handler(Exception)
    async def server_error(request: Request, error: Exception) -> JSONResponse:
        return _server_error(error)


def _server_error(error: Exception) -> JSONResponse:
    log.error("unhandled error: %s", type(error).__name__)  # the class name only
    return error_response(500, "internal_error", "Something went wrong on our side.")
