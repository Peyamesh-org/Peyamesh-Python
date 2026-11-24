"""Transport-independent parts of the API clients: configuration, request building, response parsing and
the retry policy. :mod:`peyamesh.client` and :mod:`peyamesh.async_client` only add the I/O loop."""

from __future__ import annotations

import logging
import os
import platform
import random
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional, Union
from urllib.parse import quote, urlsplit

import httpx

from ._version import __version__
from .errors import (
    APIError,
    ConfigurationError,
    InvalidRequestError,
    error_class_for,
)
from .models import Currency, Payment, Refund, VerifyResult

logger = logging.getLogger("peyamesh")

DEFAULT_BASE_URL = "https://peyamesh.ir/api/v1"
DEFAULT_TIMEOUT = 20.0
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_MAX_RETRIES = 2
MAX_RETRY_DELAY = 30.0

#: 503 codes that are safe and useful to retry. ``no_gateway_available`` is deliberately absent: the payment
#: has already been marked failed on the server, so a retry would only replay that failure.
RETRYABLE_503_CODES = frozenset({"payment_busy", "service_unavailable"})
#: Unexpected server errors and gateway timeouts. Every call is idempotent, so a retry can never duplicate.
RETRYABLE_STATUS = frozenset({500, 502, 504})

TimeoutTypes = Union[float, int, httpx.Timeout, None]
Amount = Union[int, Decimal]


@dataclass
class RequestSpec:
    method: str
    path: str
    json: Optional[dict[str, Any]] = None
    params: Optional[dict[str, Any]] = None
    idempotency_key: Optional[str] = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)


def _user_agent() -> str:
    return f"peyamesh-python/{__version__} (Python {platform.python_version()}; httpx {httpx.__version__})"


def _as_amount(value: Any, name: str = "amount") -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal, str)):
        raise TypeError(f"{name} must be an integer, got {type(value).__name__}")
    try:
        number = Decimal(str(value).strip())
    except Exception:
        raise ValueError(f"{name} must be an integer, got {value!r}") from None
    if not number.is_finite():
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if number != number.to_integral_value():
        raise ValueError(f"{name} must be a whole number (Rials/Tomans have no fractions), got {value!r}")
    amount = int(number)
    if amount <= 0:
        raise ValueError(f"{name} must be positive, got {value!r}")
    return amount


def _as_currency(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    try:
        return Currency(str(value).upper()).value
    except ValueError:
        raise ValueError(f"currency must be 'IRR' or 'IRT', got {value!r}") from None


def _check_url(value: str, name: str) -> str:
    value = (value or "").strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL, got {value!r}")
    return value


def _timeout(value: TimeoutTypes) -> httpx.Timeout:
    if isinstance(value, httpx.Timeout):
        return value
    total = DEFAULT_TIMEOUT if value is None else float(value)
    return httpx.Timeout(total, connect=min(total, DEFAULT_CONNECT_TIMEOUT))


class BaseClient:
    """Shared configuration of :class:`~peyamesh.Peyamesh` and :class:`~peyamesh.AsyncPeyamesh`."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        currency: Optional[str] = None,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        api_key = (api_key or os.environ.get("PEYAMESH_API_KEY") or "").strip()
        if not api_key:
            raise ConfigurationError(
                "Peyamesh API key is missing: pass api_key=... or set the PEYAMESH_API_KEY environment variable."
            )
        self.api_key = api_key
        self.base_url = _check_url(
            base_url or os.environ.get("PEYAMESH_BASE_URL") or DEFAULT_BASE_URL, "base_url"
        ).rstrip("/")
        self.timeout = _timeout(timeout)
        self.max_retries = DEFAULT_MAX_RETRIES if max_retries is None else max(0, int(max_retries))
        #: Default currency of amounts sent by this client. ``None`` lets the server use the shop's default.
        self.currency = _as_currency(currency)
        self._default_headers = dict(default_headers or {})

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self.base_url!r}, api_key='{self.api_key[:4]}…')"

    # ------------------------------------------------------------------ request building
    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _headers(self, spec: RequestSpec) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": _user_agent(),
            **self._default_headers,
            "Authorization": f"Bearer {self.api_key}",
            "X-Request-ID": spec.request_id,
        }
        if spec.idempotency_key:
            headers["Idempotency-Key"] = spec.idempotency_key
        return headers

    @staticmethod
    def _idempotency_key(value: Optional[str]) -> str:
        key = (value or "").strip() or f"pmpy_{uuid.uuid4().hex}"
        if len(key) > 100:
            raise ValueError("idempotency_key must be at most 100 characters")
        return key

    @staticmethod
    def _authority(value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("authority is required")
        return quote(value, safe="")

    def _create_payment_spec(
        self,
        *,
        amount: Amount,
        callback_url: str,
        currency: Optional[str],
        description: str,
        order_id: Union[str, int, None],
        mobile: str,
        email: str,
        metadata: Optional[Mapping[str, Any]],
        idempotency_key: Optional[str],
    ) -> RequestSpec:
        body: dict[str, Any] = {
            "amount": _as_amount(amount),
            "callback_url": _check_url(callback_url, "callback_url"),
        }
        resolved_currency = _as_currency(currency) or self.currency
        if resolved_currency:
            body["currency"] = resolved_currency
        if description:
            body["description"] = str(description)
        if order_id not in (None, ""):
            body["order_id"] = str(order_id)
        if mobile:
            body["mobile"] = str(mobile)
        if email:
            body["email"] = str(email)
        if metadata:
            if not isinstance(metadata, Mapping):
                raise TypeError("metadata must be a mapping")
            body["metadata"] = dict(metadata)
        return RequestSpec("POST", "payments", json=body, idempotency_key=self._idempotency_key(idempotency_key))

    def _verify_spec(self, *, authority: str, amount: Amount, currency: Optional[str]) -> RequestSpec:
        body: dict[str, Any] = {"authority": (authority or "").strip(), "amount": _as_amount(amount)}
        if not body["authority"]:
            raise ValueError("authority is required")
        resolved_currency = _as_currency(currency)
        if resolved_currency:
            body["currency"] = resolved_currency
        return RequestSpec("POST", "payments/verify", json=body)

    def _refund_spec(
        self,
        *,
        authority: str,
        amount: Optional[Amount],
        currency: Optional[str],
        reason: str,
        destination_iban: str,
        destination_card: str,
        idempotency_key: Optional[str],
    ) -> RequestSpec:
        body: dict[str, Any] = {}
        if amount is not None:
            body["amount"] = _as_amount(amount)
            resolved_currency = _as_currency(currency)
            if resolved_currency:
                body["currency"] = resolved_currency
        if reason:
            body["reason"] = str(reason)
        if destination_iban:
            body["destination_iban"] = str(destination_iban)
        if destination_card:
            body["destination_card"] = str(destination_card)
        return RequestSpec(
            "POST",
            f"payments/{self._authority(authority)}/refunds",
            json=body,
            idempotency_key=self._idempotency_key(idempotency_key),
        )

    @staticmethod
    def _remaining_refundable(error: InvalidRequestError, amount: Optional[Amount]) -> Optional[int]:
        """For a full refund (no ``amount``) rejected because other refunds are still in progress, the
        amount that *is* still refundable, as reported by the server; ``None`` otherwise."""
        if amount is not None or error.code != "refund_amount_exceeds" or not isinstance(error.details, Mapping):
            return None
        try:
            remaining = int(error.details.get("refundable_irr") or 0)
        except (TypeError, ValueError):
            return None
        return remaining if remaining > 0 else None

    # ------------------------------------------------------------------ response handling
    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def _raise_for_error(self, response: httpx.Response, spec: RequestSpec) -> None:
        if response.is_success:
            return
        body = self._decode(response)
        payload: Mapping[str, Any] = {}
        if isinstance(body, Mapping):
            inner = body.get("error")
            payload = inner if isinstance(inner, Mapping) else body
        code = str(payload.get("code") or _default_code(response.status_code))
        message = payload.get("message") or payload.get("detail") or _default_message(response.status_code)
        retry_after = _retry_after(response, payload)
        error_cls = error_class_for(response.status_code, code)
        raise error_cls(
            str(message),
            code=code,
            status_code=response.status_code,
            details=payload.get("details"),
            request_id=response.headers.get("X-Request-ID") or spec.request_id,
            retry_after=retry_after,
            body=body if body is not None else response.text[:1000],
        )

    def _parse_json(self, response: httpx.Response, spec: RequestSpec) -> Any:
        self._raise_for_error(response, spec)
        body = self._decode(response)
        if body is None:
            raise APIError(
                "پاسخ نامعتبر از درگاه پرداخت دریافت شد.",
                code="invalid_response",
                status_code=response.status_code,
                request_id=response.headers.get("X-Request-ID") or spec.request_id,
                body=response.text[:1000],
            )
        return body

    # ------------------------------------------------------------------ retry policy
    def _retry_delay(
        self,
        attempt: int,
        *,
        response: Optional[httpx.Response] = None,
        error: Optional[BaseException] = None,
    ) -> Optional[float]:
        """Seconds to wait before retrying, or ``None`` when the request must not be retried.

        Every call of this SDK is safe to repeat: payment and refund creation always carry an
        ``Idempotency-Key`` and ``verify`` is idempotent on the server (``100`` then ``101``).
        """
        if attempt >= self.max_retries:
            return None
        backoff = min(8.0, 0.5 * (2.0**attempt)) * random.uniform(0.75, 1.25)  # noqa: S311 - jitter only
        if error is not None:
            return backoff if isinstance(error, httpx.TransportError) else None
        if response is None or response.is_success:
            return None
        status = response.status_code
        if status == 429:
            body = self._decode(response)
            payload = body.get("error", body) if isinstance(body, Mapping) else {}
            wait = _retry_after(response, payload if isinstance(payload, Mapping) else {})
            if wait is None:
                return backoff
            return wait if wait <= MAX_RETRY_DELAY else None
        if status in RETRYABLE_STATUS:
            return backoff
        if status == 503:
            body = self._decode(response)
            if not isinstance(body, Mapping):
                return backoff  # proxy / maintenance page
            payload = body.get("error", body)
            code = str(payload.get("code", "")) if isinstance(payload, Mapping) else ""
            return backoff if code in RETRYABLE_503_CODES else None
        return None

    def _log(self, spec: RequestSpec, response: Optional[httpx.Response], attempt: int, elapsed: float) -> None:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "peyamesh %s /%s -> %s in %.0fms (attempt %d, request_id=%s)",
                spec.method,
                spec.path,
                response.status_code if response is not None else "error",
                elapsed * 1000,
                attempt + 1,
                spec.request_id,
            )

    # ------------------------------------------------------------------ typed results
    @staticmethod
    def _payment(body: Any, response: httpx.Response) -> Payment:
        replayed = response.headers.get("Idempotent-Replayed", "").lower() == "true"
        return Payment.from_dict(body, replayed=replayed)

    @staticmethod
    def _verify_result(body: Any) -> VerifyResult:
        return VerifyResult.from_dict(body)

    @staticmethod
    def _payments(body: Any) -> list[Payment]:
        results = body.get("results", []) if isinstance(body, Mapping) else body
        return [Payment.from_dict(item) for item in results or []]

    @staticmethod
    def _refund(body: Any) -> Refund:
        return Refund.from_dict(body)


def _retry_after(response: httpx.Response, payload: Mapping[str, Any]) -> Optional[float]:
    for raw in (payload.get("retry_after"), response.headers.get("Retry-After")):
        if raw in (None, ""):
            continue
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            continue
    return None


def _default_code(status: int) -> str:
    return {
        400: "invalid_request",
        401: "authentication_failed",
        403: "permission_denied",
        404: "not_found",
        409: "conflict",
        429: "throttled",
        503: "service_unavailable",
    }.get(status, "server_error" if status >= 500 else "error")


def _default_message(status: int) -> str:
    if status == 401:
        return "احراز هویت ناموفق بود."
    if status == 404:
        return "موردی یافت نشد."
    if status == 429:
        return "تعداد درخواست‌ها بیش از حد مجاز است."
    if status >= 500:
        return "درگاه پرداخت موقتاً در دسترس نیست. کمی بعد دوباره تلاش کنید."
    return "درخواست نامعتبر است."


__all__ = ["DEFAULT_BASE_URL", "BaseClient", "RequestSpec"]
