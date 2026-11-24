"""Exception hierarchy.

Every error raised by the SDK derives from :class:`PeyameshError`. Errors returned by the API carry the
stable machine-readable ``code`` sent by the server (for example ``amount_too_low``) together with the
human-readable Persian ``message``, so callers can branch on ``code`` and show ``message`` to buyers.

::

    PeyameshError
    ├── ConfigurationError
    ├── SignatureVerificationError
    ├── APIConnectionError
    │   └── APITimeoutError
    └── APIError
        ├── InvalidRequestError          (400 / 422)
        │   ├── AmountMismatchError
        │   └── PaymentNotPaidError
        ├── AuthenticationError          (401)
        ├── PermissionDeniedError        (403)
        ├── NotFoundError                (404)
        ├── ConflictError                (409)
        ├── RateLimitError               (429)
        ├── ServiceUnavailableError      (503)
        └── ServerError                  (5xx)
"""

from __future__ import annotations

from typing import Any, Optional


class PeyameshError(Exception):
    """Base class of every exception raised by this package."""

    def __init__(self, message: str = "", *, code: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}" if self.message else self.code


class ConfigurationError(PeyameshError):
    """The SDK or one of its integrations is misconfigured (for example, the API key is missing)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="configuration_error")


class SignatureVerificationError(PeyameshError):
    """A webhook payload could not be authenticated (bad signature, stale timestamp, malformed header)."""

    def __init__(self, message: str, *, code: str = "invalid_signature") -> None:
        super().__init__(message, code=code)


class APIConnectionError(PeyameshError):
    """The API could not be reached (DNS, TLS, connection reset, ...)."""

    def __init__(self, message: str = "ارتباط با درگاه پرداخت برقرار نشد.", *, code: str = "connection_error") -> None:
        super().__init__(message, code=code)


class APITimeoutError(APIConnectionError):
    """The API did not answer in time. The request may or may not have been processed by the server."""

    def __init__(self, message: str = "پاسخی از درگاه پرداخت در زمان مقرر دریافت نشد.") -> None:
        super().__init__(message, code="timeout")


class APIError(PeyameshError):
    """The API answered with an error status."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "error",
        status_code: Optional[int] = None,
        details: Any = None,
        request_id: Optional[str] = None,
        retry_after: Optional[float] = None,
        body: Any = None,
    ) -> None:
        super().__init__(message, code=code)
        self.status_code = status_code
        self.details = details
        self.request_id = request_id
        self.retry_after = retry_after
        self.body = body

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, status_code={self.status_code!r}, "
            f"message={self.message!r}, request_id={self.request_id!r})"
        )


class InvalidRequestError(APIError):
    """The request was rejected: validation errors, amount limits, invalid callback URL, ..."""


class AmountMismatchError(InvalidRequestError):
    """The amount sent to ``verify`` does not match the amount of the payment."""


class PaymentNotPaidError(InvalidRequestError):
    """``verify`` was called for a payment that has not been paid (yet).

    :attr:`payment_status` holds the current status reported by the server, e.g. ``failed``,
    ``expired``, ``pending`` or ``unknown`` (the bank result is still being confirmed).
    """

    @property
    def payment_status(self) -> Optional[str]:
        if isinstance(self.details, dict):
            status = self.details.get("status")
            return str(status) if status is not None else None
        return None


class AuthenticationError(APIError):
    """The API key is missing, invalid, revoked, or not allowed from this IP address."""


class PermissionDeniedError(APIError):
    """The API key is valid but not allowed to perform this action."""


class NotFoundError(APIError):
    """The requested payment or refund does not exist for this API key."""


class ConflictError(APIError):
    """The request conflicts with the current state, e.g. an ``Idempotency-Key`` reused with other data."""


class RateLimitError(APIError):
    """Too many requests. :attr:`retry_after` holds the number of seconds to wait, when known."""


class ServiceUnavailableError(APIError):
    """The service is temporarily unable to handle the request (for example ``payment_busy``)."""


class ServerError(APIError):
    """An unexpected server-side error."""


_BY_CODE: dict[str, type[APIError]] = {
    "amount_mismatch": AmountMismatchError,
    "payment_not_paid": PaymentNotPaidError,
    "idempotency_mismatch": ConflictError,
}

_BY_STATUS: dict[int, type[APIError]] = {
    400: InvalidRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    413: InvalidRequestError,
    415: InvalidRequestError,
    422: InvalidRequestError,
    429: RateLimitError,
    503: ServiceUnavailableError,
}

#: Error codes the server uses for authentication problems (sent with 401, or 403 by some proxies).
AUTH_CODES = frozenset(
    {
        "invalid_api_key",
        "revoked_api_key",
        "ip_not_allowed",
        "merchant_not_found",
        "merchant_inactive",
        "not_authenticated",
        "authentication_failed",
    }
)


def error_class_for(status_code: int, code: str) -> type[APIError]:
    if code in _BY_CODE:
        return _BY_CODE[code]
    if code in AUTH_CODES:
        return AuthenticationError
    if status_code in _BY_STATUS:
        return _BY_STATUS[status_code]
    if status_code >= 500:
        return ServerError
    if 400 <= status_code < 500:
        return InvalidRequestError
    return APIError


__all__ = [
    "APIConnectionError",
    "APIError",
    "APITimeoutError",
    "AmountMismatchError",
    "AuthenticationError",
    "ConfigurationError",
    "ConflictError",
    "InvalidRequestError",
    "NotFoundError",
    "PaymentNotPaidError",
    "PermissionDeniedError",
    "PeyameshError",
    "RateLimitError",
    "ServerError",
    "ServiceUnavailableError",
    "SignatureVerificationError",
    "error_class_for",
]
