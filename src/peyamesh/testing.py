"""Test helpers: an in-memory imitation of the Peyamesh API — no network, no API key needed.

Use it to test your checkout flow end to end::

    from peyamesh.testing import FakeGateway

    gateway = FakeGateway()
    client = gateway.client()                       # a real Peyamesh client wired to the fake

    payment = client.create_payment(10_000, "https://shop.test/peyamesh/callback/")
    gateway.pay(payment.authority)                  # the buyer pays at the bank
    params = gateway.callback_params(payment.authority)   # {"Authority": ..., "Status": "OK"}
    result = client.verify_payment(payment.authority, 10_000)
    assert result.code == 100

    body, headers = gateway.webhook("payment.succeeded", payment.authority)  # a signed webhook delivery

Failures can be injected with :meth:`FakeGateway.fail_next` and :meth:`FakeGateway.network_error_next`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import secrets
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

import httpx

from .async_client import AsyncPeyamesh
from .client import Peyamesh
from .contrib._flow import with_query
from .webhooks import generate_test_header

TEST_API_KEY = "test_00000000-0000-4000-8000-000000000000"
TEST_WEBHOOK_SECRET = "whsec_test_secret"  # noqa: S105 - public test value
TEST_BASE_URL = "https://peyamesh.test/api/v1"

_PAID = {"paid", "verified", "partially_refunded", "refunded"}
_SUCCESS = {"verified", "partially_refunded", "refunded"}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: Optional[dt.datetime]) -> Optional[str]:
    return value.isoformat() if value else None


@dataclass
class _Injected:
    status: int
    body: Any
    times: int
    path: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)


class FakeGateway:
    """In-memory stand-in for the Peyamesh API, used through :class:`httpx.MockTransport`.

    It implements the merchant API (create / verify / get / unverified / refunds) with the same status
    codes, error codes, idempotency rules and Rial/Toman conversion as the real service.
    """

    def __init__(
        self,
        *,
        api_key: str = TEST_API_KEY,
        webhook_secret: str = TEST_WEBHOOK_SECRET,
        base_url: str = TEST_BASE_URL,
        livemode: bool = False,
        default_currency: str = "IRR",
        fee_irr: int = 0,
        min_amount_irr: int = 1_000,
        auto_complete_refunds: bool = True,
    ) -> None:
        self.api_key = api_key
        self.webhook_secret = webhook_secret
        self.base_url = base_url.rstrip("/")
        self.livemode = livemode
        self.default_currency = default_currency
        self.fee_irr = fee_irr
        self.min_amount_irr = min_amount_irr
        #: With ``False`` refunds stay ``approved`` until :meth:`complete_refund` (like refunds that need
        #: manual processing), and count as "in progress" for later refund requests.
        self.auto_complete_refunds = auto_complete_refunds
        self.payments: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        #: Every request received, as ``(method, path, headers, json_body)``.
        self.requests: list[tuple[str, str, Mapping[str, str], Any]] = []
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._refund_keys: dict[str, str] = {}
        self._injected: list[_Injected] = []
        self._network_errors: list[tuple[Optional[str], bool, type[httpx.TransportError]]] = []
        self._ref_ids = itertools.count(100_000_000_001)
        self._events = itertools.count(1)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ clients
    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self, **kwargs: Any) -> Peyamesh:
        kwargs.setdefault("api_key", self.api_key)
        kwargs.setdefault("base_url", self.base_url)
        kwargs.setdefault("http_client", httpx.Client(transport=self.transport))
        return Peyamesh(**kwargs)

    def async_client(self, **kwargs: Any) -> AsyncPeyamesh:
        kwargs.setdefault("api_key", self.api_key)
        kwargs.setdefault("base_url", self.base_url)
        kwargs.setdefault("http_client", httpx.AsyncClient(transport=self.transport))
        return AsyncPeyamesh(**kwargs)

    # ------------------------------------------------------------------ failure injection
    def fail_next(
        self,
        status: int = 503,
        code: str = "service_unavailable",
        message: str = "سرویس موقتاً در دسترس نیست.",
        *,
        times: int = 1,
        path: Optional[str] = None,
        details: Any = None,
        retry_after: Optional[int] = None,
    ) -> None:
        """Answer the next ``times`` requests (optionally only those whose path contains ``path``) with an
        error in the real API format."""
        error: dict[str, Any] = {"code": code, "message": message}
        if details is not None:
            error["details"] = details
        if retry_after is not None:
            error["retry_after"] = retry_after
        self._injected.append(_Injected(status, {"error": error}, times, path))

    def network_error_next(
        self, *, times: int = 1, path: Optional[str] = None, timeout: bool = False, after_processing: bool = False
    ) -> None:
        """Make the next requests fail at the network level (``httpx.ConnectError`` / ``ReadTimeout``).

        With ``after_processing=True`` the request is processed first and only the *response* is lost —
        the classic "timed out but the server did it" case that idempotency keys exist for.
        """
        exc: type[httpx.TransportError] = httpx.ReadTimeout if timeout else httpx.ConnectError
        for _ in range(times):
            self._network_errors.append((path, after_processing, exc))

    # ------------------------------------------------------------------ buyer / bank simulation
    def _get(self, authority: str) -> dict[str, Any]:
        try:
            return self.payments[authority]
        except KeyError:
            raise KeyError(f"unknown authority {authority!r}") from None

    def pay(self, authority: str, *, card_pan: str = "603799******7890") -> dict[str, Any]:
        """The buyer pays successfully at the bank (status becomes ``paid``, awaiting verification)."""
        with self._lock:
            p = self._get(authority)
            p.update(
                status="paid",
                paid_at=_iso(_now()),
                ref_id=next(self._ref_ids),
                card_pan=card_pan,
                card_hash=hashlib.sha256(card_pan.encode()).hexdigest(),
            )
            return dict(p)

    def fail(
        self, authority: str, *, code: str = "canceled_by_user", message: str = "پرداخت توسط کاربر لغو شد."
    ) -> None:
        with self._lock:
            self._get(authority).update(status="failed", failure_code=code, failure_message=message)

    def expire(self, authority: str) -> None:
        with self._lock:
            self._get(authority).update(
                status="expired", failure_code="expired", failure_message="مهلت پرداخت به پایان رسید."
            )

    def mark_unknown(self, authority: str) -> None:
        """The bank result is still being confirmed (the buyer returns with ``pm_status=unknown``)."""
        with self._lock:
            self._get(authority)["status"] = "unknown"

    def callback_params(self, authority: str) -> dict[str, str]:
        p = self._get(authority)
        params = {"Authority": authority, "Status": "OK" if p["status"] in _PAID else "NOK"}
        if p["status"] == "unknown":
            params["pm_status"] = "unknown"
        return params

    def callback_url(self, authority: str) -> str:
        """The full URL the buyer is sent back to (``callback_url`` + ``Authority`` / ``Status``)."""
        return with_query(self._get(authority)["_callback_url"], self.callback_params(authority))

    def webhook(
        self,
        event_type: str,
        authority: Optional[str] = None,
        *,
        data: Optional[Mapping[str, Any]] = None,
        event_id: Optional[str] = None,
        timestamp: Optional[int] = None,
        secret: Optional[str] = None,
    ) -> tuple[bytes, dict[str, str]]:
        """Build a signed webhook delivery ``(body, headers)`` exactly as the service sends it."""
        payload_data = (
            dict(data) if data is not None else {"object": "payment", **self._public(self._get(authority or ""))}
        )
        event = {
            "id": event_id or f"evt_{next(self._events):024d}",
            "type": event_type,
            "created_at": _iso(_now()),
            "livemode": self.livemode,
            "data": payload_data,
        }
        body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            "Peyamesh-Event-Id": str(event["id"]),
            "Peyamesh-Event-Type": event_type,
            "Peyamesh-Delivery-Attempt": "1",
            "Peyamesh-Signature": generate_test_header(body, secret or self.webhook_secret, timestamp=timestamp),
        }
        return body, headers

    # ------------------------------------------------------------------ HTTP handling
    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = urlsplit(str(request.url)).path
        prefix = urlsplit(self.base_url).path
        rel = path[len(prefix) :].strip("/") if path.startswith(prefix) else path.strip("/")
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = None
        with self._lock:
            self.requests.append((request.method, rel, dict(request.headers), body))
            for i, (target, after, exc) in enumerate(self._network_errors):
                if target and target not in rel:
                    continue
                self._network_errors.pop(i)
                if after:
                    self._dispatch(request, rel, body)
                raise exc("simulated network failure", request=request)
            for injected in self._injected:
                if injected.path and injected.path not in rel:
                    continue
                injected.times -= 1
                if injected.times <= 0:
                    self._injected.remove(injected)
                return httpx.Response(injected.status, json=injected.body, headers=injected.headers)
            return self._dispatch(request, rel, body)

    def _dispatch(self, request: httpx.Request, rel: str, body: Any) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {self.api_key}":
            return self._error(401, "invalid_api_key", "کلید API معتبر نیست.")
        parts = [unquote(p) for p in rel.split("/") if p]
        method = request.method
        if parts == ["payments"] and method == "POST":
            return self._create(request, body or {})
        if parts == ["payments", "verify"] and method == "POST":
            return self._verify(body or {})
        if parts == ["payments", "unverified"] and method == "GET":
            results = [self._public(p) for p in self.payments.values() if p["status"] == "paid"]
            return httpx.Response(200, json={"results": results[::-1][:100]})
        if len(parts) == 2 and parts[0] == "payments" and method == "GET":
            p = self.payments.get(parts[1])
            if p is None:
                return self._error(404, "payment_not_found", "تراکنش یافت نشد.")
            return httpx.Response(200, json=self._public(p, with_url=p["status"] in ("created", "pending")))
        if len(parts) == 3 and parts[0] == "payments" and parts[2] == "refunds" and method == "POST":
            return self._refund(request, parts[1], body or {})
        if len(parts) == 2 and parts[0] == "refunds" and method == "GET":
            refund = self.refunds.get(parts[1])
            if refund is None:
                return self._error(404, "refund_not_found", "بازپرداخت یافت نشد.")
            return httpx.Response(200, json=refund)
        return self._error(404, "not_found", "موردی یافت نشد.")

    @staticmethod
    def _error(status: int, code: str, message: str, details: Any = None) -> httpx.Response:
        error: dict[str, Any] = {"code": code, "message": message}
        if details is not None:
            error["details"] = details
        return httpx.Response(status, json={"error": error})

    @staticmethod
    def _to_rial(amount: int, currency: str) -> int:
        return amount * 10 if currency == "IRT" else amount

    def _public(self, p: Mapping[str, Any], *, with_url: bool = False) -> dict[str, Any]:
        data = {k: v for k, v in p.items() if not k.startswith("_")}
        data["refunded"] = p["refunded_irr"] // 10 if p["currency"] == "IRT" else p["refunded_irr"]
        if with_url:
            data["payment_url"] = f"https://pay.peyamesh.test/pay/{p['authority']}"
        return data

    def _create(self, request: httpx.Request, body: Mapping[str, Any]) -> httpx.Response:
        errors: dict[str, list[str]] = {}
        amount = body.get("amount")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1:
            errors["amount"] = ["مبلغ معتبر نیست."]
        callback = body.get("callback_url")
        if not isinstance(callback, str) or not callback.startswith(("http://", "https://")):
            errors["callback_url"] = ["آدرس بازگشت معتبر نیست."]
        currency = body.get("currency") or self.default_currency
        if currency not in ("IRR", "IRT"):
            errors["currency"] = ["واحد پول معتبر نیست."]
        if errors:
            return self._error(400, "validation_error", "اطلاعات ارسالی معتبر نیست.", errors)
        amount_irr = self._to_rial(int(amount or 0), currency)
        if amount_irr < self.min_amount_irr:
            return self._error(
                400, "amount_too_low", "مبلغ تراکنش کمتر از حداقل مجاز است.", {"min_amount_irr": self.min_amount_irr}
            )
        key = request.headers.get("Idempotency-Key")
        fingerprint = json.dumps([amount_irr, callback, body.get("order_id", ""), body.get("description", "")])
        if key and key in self._idempotency:
            authority, previous = self._idempotency[key]
            if previous != fingerprint:
                return self._error(
                    409, "idempotency_mismatch", "این کلید یکتایی قبلاً با داده‌های دیگری استفاده شده است."
                )
            existing = self.payments[authority]
            return httpx.Response(
                200,
                json=self._public(existing, with_url=existing["status"] in ("created", "pending")),
                headers={"Idempotent-Replayed": "true"},
            )
        authority = "A" + secrets.token_hex(18)[:35]
        now = _now()
        p: dict[str, Any] = {
            "authority": authority,
            "status": "created",
            "livemode": self.livemode,
            "amount": amount,
            "currency": currency,
            "amount_irr": amount_irr,
            "fee_irr": self.fee_irr,
            "fee_bearer": "merchant",
            "payable_irr": amount_irr,
            "net_irr": amount_irr - self.fee_irr,
            "refunded_irr": 0,
            "order_id": str(body.get("order_id", "")),
            "description": str(body.get("description", "")),
            "mobile": str(body.get("mobile", "")),
            "email": str(body.get("email", "")),
            "metadata": dict(body.get("metadata") or {}),
            "ref_id": None,
            "card_pan": "",
            "card_hash": "",
            "expires_at": _iso(now + dt.timedelta(minutes=20)),
            "created_at": _iso(now),
            "paid_at": None,
            "verified_at": None,
            "failure_code": "",
            "failure_message": "",
            "_callback_url": callback,
        }
        self.payments[authority] = p
        if key:
            self._idempotency[key] = (authority, fingerprint)
        return httpx.Response(201, json=self._public(p, with_url=True))

    def _verify(self, body: Mapping[str, Any]) -> httpx.Response:
        p = self.payments.get(str(body.get("authority", "")))
        if p is None:
            return self._error(404, "payment_not_found", "تراکنش یافت نشد.")
        amount = body.get("amount")
        if (
            not isinstance(amount, int)
            or self._to_rial(amount, body.get("currency") or p["currency"]) != p["amount_irr"]
        ):
            return self._error(400, "amount_mismatch", "مبلغ ارسالی با مبلغ تراکنش مطابقت ندارد.")
        if p["status"] == "paid":
            p.update(status="verified", verified_at=_iso(_now()))
            return httpx.Response(200, json={**self._public(p), "code": 100, "message": "تراکنش تأیید شد."})
        if p["status"] in _SUCCESS:
            return httpx.Response(200, json={**self._public(p), "code": 101, "message": "تراکنش قبلاً تأیید شده است."})
        return self._error(400, "payment_not_paid", "پرداخت انجام نشده یا ناموفق بوده است.", {"status": p["status"]})

    def _refund(self, request: httpx.Request, authority: str, body: Mapping[str, Any]) -> httpx.Response:
        p = self.payments.get(authority)
        if p is None:
            return self._error(404, "payment_not_found", "تراکنش یافت نشد.")
        # No amount = everything still refundable, computed below (net of refunds still in progress).
        amount_irr: Optional[int] = (
            self._to_rial(int(body["amount"]), body.get("currency") or p["currency"]) if "amount" in body else None
        )
        key = request.headers.get("Idempotency-Key")
        if key and key in self._refund_keys:
            existing = self.refunds[self._refund_keys[key]]
            # A replayed full refund matches on the payment alone: what is refundable has changed since.
            if existing["authority"] != authority or (amount_irr is not None and existing["amount_irr"] != amount_irr):
                return self._error(
                    409, "idempotency_mismatch", "این کلید یکتایی قبلاً برای درخواست دیگری استفاده شده است."
                )
            return httpx.Response(201, json=existing)
        if p["status"] not in _SUCCESS:
            return self._error(400, "payment_not_refundable", "فقط پرداخت‌های موفق قابل بازپرداخت هستند.")
        in_progress = sum(
            r["amount_irr"]
            for r in self.refunds.values()
            if r["authority"] == authority and r["status"] in ("requested", "approved", "processing")
        )
        available = p["amount_irr"] - p["refunded_irr"] - in_progress
        if amount_irr is None:
            amount_irr = available
        if amount_irr <= 0 or amount_irr > available:
            return self._error(
                400,
                "refund_amount_exceeds",
                "مبلغ بازپرداخت بیش از حد مجاز است.",
                {"refundable_irr": max(available, 0)},
            )
        refund_id = "rf_" + secrets.token_hex(10)
        now = _iso(_now())
        refund = {
            "id": refund_id,
            "authority": authority,
            "amount_irr": amount_irr,
            "returned_fee_irr": 0,
            "reason": str(body.get("reason", "")),
            "status": "approved",
            "method": "psp_reverse",
            "channel": "api",
            "destination_iban": str(body.get("destination_iban", "")),
            "destination_card": str(body.get("destination_card", "")),
            "bank_reference": "R" + refund_id[3:11],
            "failure_reason": "",
            "created_at": now,
            "reviewed_at": now,
            "processed_at": None,
        }
        self.refunds[refund_id] = refund
        if key:
            self._refund_keys[key] = refund_id
        if self.auto_complete_refunds:
            self.complete_refund(refund_id)
        return httpx.Response(201, json=refund)

    def complete_refund(self, refund_id: str) -> dict[str, Any]:
        """The money of an ``approved`` refund reaches the buyer (status ``succeeded``)."""
        with self._lock:
            refund = self.refunds[refund_id]
            if refund["status"] != "succeeded":
                p = self._get(refund["authority"])
                refund.update(status="succeeded", processed_at=_iso(_now()))
                p["refunded_irr"] += refund["amount_irr"]
                p["status"] = "refunded" if p["refunded_irr"] >= p["amount_irr"] else "partially_refunded"
            return dict(refund)

    # ------------------------------------------------------------------ inspection
    def requests_to(self, path: str, method: Optional[str] = None) -> list[tuple[str, str, Mapping[str, str], Any]]:
        return [r for r in self.requests if r[1] == path.strip("/") and (method is None or r[0] == method)]


__all__ = ["TEST_API_KEY", "TEST_BASE_URL", "TEST_WEBHOOK_SECRET", "FakeGateway"]
