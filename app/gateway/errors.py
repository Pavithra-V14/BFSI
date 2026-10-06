"""Typed gateway errors (spec section 5.2).

Provider and LiteLLM errors are mapped to these classes. The messages carry a
short reason and a status code only: provider error text can echo parts of a key
or of the request, so it is never copied.
"""

from __future__ import annotations

import asyncio

import httpx
import litellm


class GatewayError(Exception):
    reason = "error"
    # Tokens the provider already counted for a failed or invalid answer; they still count
    # toward the limiter (a failed answer is not free).
    used_tokens = 0

    def __init__(self, reason: str | None = None, *, status: int | None = None) -> None:
        self.reason = reason or type(self).reason
        self.status = status
        super().__init__(self.reason)


class RateLimited(GatewayError):
    reason = "rate_limited"

    def __init__(self, *, retry_after: float | None = None, status: int | None = 429) -> None:
        super().__init__(status=status)
        self.retry_after = retry_after


class CallTimeout(GatewayError):
    reason = "timeout"


class ProviderUnavailable(GatewayError):
    reason = "provider_unavailable"


class CreditExhausted(ProviderUnavailable):
    reason = "credit_exhausted"


class BadEmbedding(ProviderUnavailable):
    """A key answered with vectors we cannot use (wrong count, wrong size, empty or not finite).

    It is treated like a failing key, so the next key is tried.
    """

    reason = "bad_embedding"


class InternalError(GatewayError):
    """An unexpected exception inside the gateway or its parsing code: a bug, not an outage.

    It is never retried and never counts against a provider. Only the exception class name is
    kept, never its message, because messages can hold values.
    """

    reason = "internal_error"

    def __init__(self, exception_name: str = "") -> None:
        super().__init__()
        self.exception_name = exception_name


class AuthFailed(GatewayError):
    reason = "auth_failed"


class BadRequest(GatewayError):
    reason = "bad_request"


class ContextWindowExceeded(GatewayError):
    """The prompt does not fit. Never retried (spec 5.2)."""

    reason = "context_window_exceeded"


class SchemaSurprise(GatewayError):
    """A provider answered, but not in the shape we expected."""

    reason = "schema_surprise"


class InvalidOutput(GatewayError):
    reason = "invalid_output"


class AllProvidersFailed(GatewayError):
    """Every step of a chain failed. The caller shows 'temporarily unavailable'."""

    reason = "all_providers_failed"

    def __init__(self, skipped: list[tuple[str, str]]) -> None:
        super().__init__()
        self.skipped = skipped


class EmbeddingUnavailable(AllProvidersFailed):
    reason = "embedding_unavailable"


RETRYABLE = (CallTimeout, ProviderUnavailable)

# Exceptions that mean "the provider or the network is not working". Anything else that
# reaches classify() is treated as a bug in our own code (InternalError).
TRANSPORT_ERRORS = (
    httpx.TransportError,
    OSError,
    litellm.APIConnectionError,
    litellm.ServiceUnavailableError,
    litellm.InternalServerError,
    litellm.APIError,
)


def retry_after_seconds(headers: object) -> float | None:
    try:
        value = headers.get("retry-after")  # type: ignore[attr-defined]
        return float(value) if value is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def status_to_error(status: int, headers: object = None) -> GatewayError:
    """Map an HTTP status from a direct (httpx) provider call."""
    if status == 429:
        return RateLimited(retry_after=retry_after_seconds(headers))
    if status == 402:
        return CreditExhausted(status=status)
    if status in (401, 403):
        return AuthFailed(status=status)
    if status == 408:
        return CallTimeout(status=status)
    if status >= 500:
        return ProviderUnavailable(status=status)
    return BadRequest(status=status)


def classify(exc: BaseException) -> GatewayError:
    """Turn any exception raised by LiteLLM or httpx into a gateway error."""
    if isinstance(exc, GatewayError):
        return exc
    if isinstance(exc, litellm.ContextWindowExceededError):
        return ContextWindowExceeded(status=400)
    if isinstance(exc, litellm.RateLimitError):
        response = getattr(exc, "response", None)
        return RateLimited(retry_after=retry_after_seconds(getattr(response, "headers", None)))
    if isinstance(exc, (litellm.AuthenticationError, litellm.PermissionDeniedError)):
        return AuthFailed(status=getattr(exc, "status_code", 401))
    if isinstance(exc, (litellm.Timeout, httpx.TimeoutException, asyncio.TimeoutError)):
        return CallTimeout(status=408)
    if isinstance(exc, (litellm.BadRequestError, litellm.UnprocessableEntityError)):
        return BadRequest(status=getattr(exc, "status_code", 400))
    if isinstance(exc, litellm.NotFoundError):
        return BadRequest("model_not_found", status=404)
    if isinstance(exc, httpx.HTTPStatusError):
        return status_to_error(exc.response.status_code, exc.response.headers)
    if isinstance(exc, TRANSPORT_ERRORS):
        return ProviderUnavailable(status=getattr(exc, "status_code", None))
    return InternalError(type(exc).__name__)
