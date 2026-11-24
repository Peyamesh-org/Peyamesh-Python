"""Synchronous API client."""

from __future__ import annotations

import time
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Optional, Union

import httpx

from ._base import Amount, BaseClient, RequestSpec, TimeoutTypes
from .callback import CallbackParams, parse_callback
from .errors import APIConnectionError, APITimeoutError, InvalidRequestError
from .models import Payment, Refund, VerifyResult


class Peyamesh(BaseClient):
    """Thread-safe synchronous client of the Peyamesh payment API.

    ::

        from peyamesh import Peyamesh

        client = Peyamesh(api_key="...", currency="IRT")
        payment = client.create_payment(amount=245_000, callback_url="https://shop.ir/pay/back", order_id="1048")
        # redirect the buyer to payment.payment_url

        # on return to callback_url:
        result = client.verify_payment(authority, amount=245_000)   # amount from YOUR database

    Network errors, timeouts, ``429`` and transient ``5xx`` answers are retried automatically
    (``max_retries`` times, exponential backoff); every call is idempotent so retries are safe.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        currency: Optional[str] = None,
        default_headers: Optional[Mapping[str, str]] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        super().__init__(
            api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            currency=currency,
            default_headers=default_headers,
        )
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.Client(timeout=self.timeout, follow_redirects=False)

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def __enter__(self) -> Peyamesh:
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()

    # ------------------------------------------------------------------ transport
    def _request(self, spec: RequestSpec) -> tuple[Any, httpx.Response]:
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                response = self._http.request(
                    spec.method,
                    self._url(spec.path),
                    json=spec.json,
                    params=spec.params,
                    headers=self._headers(spec),
                    timeout=self.timeout,
                )
            except httpx.TransportError as exc:
                self._log(spec, None, attempt, time.monotonic() - started)
                delay = self._retry_delay(attempt, error=exc)
                if delay is None:
                    if isinstance(exc, httpx.TimeoutException):
                        raise APITimeoutError() from exc
                    raise APIConnectionError() from exc
            else:
                self._log(spec, response, attempt, time.monotonic() - started)
                delay = self._retry_delay(attempt, response=response)
                if delay is None:
                    return self._parse_json(response, spec), response
            time.sleep(delay)
            attempt += 1

    # ------------------------------------------------------------------ payments
    def create_payment(
        self,
        amount: Amount,
        callback_url: str,
        *,
        currency: Optional[str] = None,
        description: str = "",
        order_id: Union[str, int, None] = None,
        mobile: str = "",
        email: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Payment:
        """Create a payment and return it with its ``payment_url`` (redirect the buyer there).

        ``idempotency_key`` (optional) makes the call safe to repeat across processes: the same key
        returns the same payment instead of creating a new one. A random key is used otherwise, which still
        protects the SDK's own automatic retries.
        """
        spec = self._create_payment_spec(
            amount=amount,
            callback_url=callback_url,
            currency=currency,
            description=description,
            order_id=order_id,
            mobile=mobile,
            email=email,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
        body, response = self._request(spec)
        return self._payment(body, response)

    def verify_payment(self, authority: str, amount: Amount, *, currency: Optional[str] = None) -> VerifyResult:
        """Confirm a paid payment. **Required** before delivering the order.

        ``amount`` must come from your own records (never from the return URL). Returns code ``100``
        when verified now and ``101`` when it had already been verified — both mean success.

        :raises PaymentNotPaidError: the payment is not paid (see ``error.payment_status``).
        :raises AmountMismatchError: ``amount`` differs from the payment amount.
        """
        body, _ = self._request(self._verify_spec(authority=authority, amount=amount, currency=currency))
        return self._verify_result(body)

    def verify_callback(
        self, params: Any, amount: Amount, *, currency: Optional[str] = None
    ) -> tuple[CallbackParams, VerifyResult]:
        """Parse the return parameters (``request.GET``, ``request.query_params``, dict ...) and verify.

        The ``Status`` parameter is deliberately ignored: the server is the only source of truth.
        """
        callback = parse_callback(params)
        return callback, self.verify_payment(callback.authority, amount, currency=currency)

    def get_payment(self, authority: str) -> Payment:
        body, response = self._request(RequestSpec("GET", f"payments/{self._authority(authority)}"))
        return self._payment(body, response)

    def list_unverified_payments(self) -> list[Payment]:
        """Payments that were paid but never verified (most recent first, at most 100).

        Run this periodically and verify each one you know about, so that a buyer who closed the browser
        right after paying still gets the order.
        """
        body, _ = self._request(RequestSpec("GET", "payments/unverified"))
        return self._payments(body)

    # ------------------------------------------------------------------ refunds
    def create_refund(
        self,
        authority: str,
        *,
        amount: Optional[Amount] = None,
        currency: Optional[str] = None,
        reason: str = "",
        destination_iban: str = "",
        destination_card: str = "",
        idempotency_key: Optional[str] = None,
    ) -> Refund:
        """Request a (partial) refund. Omit ``amount`` to refund everything still refundable
        (net of refunds still in progress).

        ``idempotency_key`` (optional) makes the call safe to repeat: the same key returns the same refund.
        """
        key = self._idempotency_key(idempotency_key)
        spec = self._refund_spec(
            authority=authority,
            amount=amount,
            currency=currency,
            reason=reason,
            destination_iban=destination_iban,
            destination_card=destination_card,
            idempotency_key=key,
        )
        try:
            body, _ = self._request(spec)
        except InvalidRequestError as exc:
            remaining = self._remaining_refundable(exc, amount)
            if remaining is None:
                raise
            # Older server versions reject an amount-less full refund while other refunds are still in
            # progress: refund exactly what the server says is left.
            spec = self._refund_spec(
                authority=authority,
                amount=remaining,
                currency="IRR",
                reason=reason,
                destination_iban=destination_iban,
                destination_card=destination_card,
                idempotency_key=key,
            )
            body, _ = self._request(spec)
        return self._refund(body)

    def get_refund(self, refund_id: str) -> Refund:
        body, _ = self._request(RequestSpec("GET", f"refunds/{self._authority(refund_id)}"))
        return self._refund(body)


__all__ = ["Peyamesh"]
