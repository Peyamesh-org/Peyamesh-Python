from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

import peyamesh
from peyamesh import (
    AmountMismatchError,
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PaymentNotPaidError,
    PaymentStatus,
    RateLimitError,
    ServiceUnavailableError,
)
from peyamesh.testing import TEST_API_KEY, FakeGateway


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("peyamesh.client.time.sleep", lambda s: None)


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
def client(gateway):
    with gateway.client(currency="IRT") as c:
        yield c


# --------------------------------------------------------------------------- configuration
def test_api_key_is_required(monkeypatch):
    monkeypatch.delenv("PEYAMESH_API_KEY", raising=False)
    with pytest.raises(ConfigurationError):
        peyamesh.Peyamesh()


def test_api_key_and_base_url_from_environment(monkeypatch):
    monkeypatch.setenv("PEYAMESH_API_KEY", "env-key")
    monkeypatch.setenv("PEYAMESH_BASE_URL", "https://sandbox.example.test/api/v1/")
    c = peyamesh.Peyamesh()
    assert c.api_key == "env-key"
    assert c.base_url == "https://sandbox.example.test/api/v1"
    assert "env-" in repr(c) and "env-key" not in repr(c)


def test_default_base_url():
    assert peyamesh.Peyamesh("k").base_url == "https://peyamesh.ir/api/v1"


@pytest.mark.parametrize("bad", ["ftp://x", "peyamesh.ir", ""])
def test_invalid_base_url(bad, monkeypatch):
    monkeypatch.delenv("PEYAMESH_BASE_URL", raising=False)
    if bad == "":
        assert peyamesh.Peyamesh("k", base_url=bad).base_url == peyamesh.DEFAULT_BASE_URL
        return
    with pytest.raises(ValueError):
        peyamesh.Peyamesh("k", base_url=bad)


def test_invalid_currency():
    with pytest.raises(ValueError):
        peyamesh.Peyamesh("k", currency="USD")


# --------------------------------------------------------------------------- create
def test_create_payment_sends_expected_request(gateway, client):
    payment = client.create_payment(
        245_000,
        "https://shop.test/back",
        order_id=1048,
        description="سفارش",
        mobile="09120000000",
        metadata={"cart": 7},
    )
    method, path, headers, body = gateway.requests[-1]
    assert (method, path) == ("POST", "payments")
    assert headers["authorization"] == f"Bearer {TEST_API_KEY}"
    assert headers["idempotency-key"].startswith("pmpy_")
    assert headers["x-request-id"]
    assert headers["user-agent"].startswith("peyamesh-python/")
    assert body == {
        "amount": 245_000,
        "currency": "IRT",
        "callback_url": "https://shop.test/back",
        "order_id": "1048",
        "description": "سفارش",
        "mobile": "09120000000",
        "metadata": {"cart": 7},
    }
    assert payment.status == PaymentStatus.CREATED
    assert payment.amount == 245_000 and payment.amount_irr == 2_450_000
    assert payment.payment_url and payment.authority in payment.payment_url
    assert payment.created_at is not None and payment.created_at.tzinfo is not None
    assert not payment.replayed


def test_currency_override_per_call(gateway, client):
    payment = client.create_payment(50_000, "https://shop.test/back", currency="IRR")
    assert gateway.requests[-1][3]["currency"] == "IRR"
    assert payment.amount_irr == 50_000


def test_currency_omitted_when_not_configured(gateway):
    c = gateway.client()
    c.create_payment(50_000, "https://shop.test/back")
    assert "currency" not in gateway.requests[-1][3]


@pytest.mark.parametrize(
    ("amount", "exc"),
    [
        (0, ValueError),
        (-5, ValueError),
        (10.5, TypeError),
        (True, TypeError),
        ("12.5", ValueError),
        (Decimal("Infinity"), ValueError),
        (Decimal("NaN"), ValueError),
        ("abc", ValueError),
    ],
)
def test_create_payment_rejects_bad_amounts(client, amount, exc):
    with pytest.raises(exc):
        client.create_payment(amount, "https://shop.test/back")


def test_create_payment_rejects_relative_callback(client):
    with pytest.raises(ValueError):
        client.create_payment(10_000, "/back")


def test_idempotency_replay(gateway, client):
    first = client.create_payment(10_000, "https://shop.test/back", idempotency_key="order-1")
    again = client.create_payment(10_000, "https://shop.test/back", idempotency_key="order-1")
    assert again.authority == first.authority
    assert again.replayed
    with pytest.raises(ConflictError) as info:
        client.create_payment(20_000, "https://shop.test/back", idempotency_key="order-1")
    assert info.value.code == "idempotency_mismatch"
    assert info.value.status_code == 409


def test_server_validation_errors_are_typed(gateway, client):
    with pytest.raises(InvalidRequestError) as info:
        client.create_payment(10, "https://shop.test/back")  # 100 IRR < minimum
    err = info.value
    assert err.code == "amount_too_low"
    assert err.details == {"min_amount_irr": 1_000}
    assert err.request_id
    assert "amount_too_low" in str(err)


def test_invalid_api_key(gateway):
    c = gateway.client(api_key="wrong")
    with pytest.raises(AuthenticationError) as info:
        c.get_payment("A" * 36)
    assert info.value.code == "invalid_api_key"
    assert info.value.status_code == 401


# --------------------------------------------------------------------------- verify / get
def test_verify_flow(gateway, client):
    payment = client.create_payment(10_000, "https://shop.test/back")
    with pytest.raises(PaymentNotPaidError) as info:
        client.verify_payment(payment.authority, 10_000)
    assert info.value.payment_status == "created"

    gateway.pay(payment.authority)
    result = client.verify_payment(payment.authority, 10_000)
    assert result.code == 100 and not result.already_verified
    assert result.payment.status == PaymentStatus.VERIFIED
    assert result.payment.is_verified and result.payment.is_paid
    assert result.ref_id

    again = client.verify_payment(payment.authority, 10_000)
    assert again.code == 101 and again.already_verified


def test_verify_amount_mismatch(gateway, client):
    payment = client.create_payment(10_000, "https://shop.test/back")
    gateway.pay(payment.authority)
    with pytest.raises(AmountMismatchError):
        client.verify_payment(payment.authority, 9_000)


def test_verify_callback_ignores_status_param(gateway, client):
    payment = client.create_payment(10_000, "https://shop.test/back")
    gateway.pay(payment.authority)
    forged = {"Authority": payment.authority, "Status": "NOK"}
    callback, result = client.verify_callback(forged, 10_000)
    assert callback.authority == payment.authority and not callback.is_ok
    assert result.code == 100


def test_get_payment_and_not_found(gateway, client):
    payment = client.create_payment(10_000, "https://shop.test/back")
    fetched = client.get_payment(payment.authority)
    assert fetched.authority == payment.authority and fetched.is_open
    with pytest.raises(NotFoundError):
        client.get_payment("A" + "0" * 35)


def test_list_unverified(gateway, client):
    a = client.create_payment(10_000, "https://shop.test/back")
    b = client.create_payment(20_000, "https://shop.test/back")
    gateway.pay(a.authority)
    gateway.pay(b.authority)
    client.verify_payment(b.authority, 20_000)
    unverified = client.list_unverified_payments()
    assert [p.authority for p in unverified] == [a.authority]
    assert unverified[0].needs_verification


# --------------------------------------------------------------------------- refunds
def test_refunds(gateway, client):
    payment = client.create_payment(10_000, "https://shop.test/back")
    gateway.pay(payment.authority)
    client.verify_payment(payment.authority, 10_000)
    refund = client.create_refund(payment.authority, amount=4_000, reason="مرجوعی", idempotency_key="rf-1")
    assert refund.amount_irr == 40_000 and refund.is_final
    assert gateway.requests[-1][3] == {"amount": 4_000, "reason": "مرجوعی"}
    assert client.create_refund(payment.authority, amount=4_000, idempotency_key="rf-1").id == refund.id
    assert client.get_refund(refund.id).id == refund.id
    assert client.get_payment(payment.authority).status == PaymentStatus.PARTIALLY_REFUNDED


# --------------------------------------------------------------------------- retries
def test_retries_connection_errors_with_same_keys(gateway, client):
    gateway.network_error_next(times=2, path="payments")
    payment = client.create_payment(10_000, "https://shop.test/back")
    assert payment.authority
    creates = gateway.requests_to("payments", "POST")
    assert len(creates) == 3
    assert len({r[2]["idempotency-key"] for r in creates}) == 1
    assert len({r[2]["x-request-id"] for r in creates}) == 1
    assert len(gateway.payments) == 1


def test_timeout_after_processing_does_not_duplicate(gateway, client):
    gateway.network_error_next(timeout=True, after_processing=True)
    payment = client.create_payment(10_000, "https://shop.test/back")
    assert len(gateway.payments) == 1
    assert payment.replayed  # the retry was answered from the idempotency record


def test_gives_up_after_max_retries(gateway):
    c = gateway.client(max_retries=1)
    gateway.network_error_next(times=2)
    with pytest.raises(APIConnectionError):
        c.get_payment("A" * 36)
    gateway.network_error_next(times=2, timeout=True)
    with pytest.raises(APITimeoutError):
        c.get_payment("A" * 36)


def test_rate_limit_is_retried_then_raised(gateway, client):
    gateway.fail_next(429, "throttled", "بیش از حد", retry_after=1)
    payment = client.create_payment(10_000, "https://shop.test/back")
    assert payment.authority

    gateway.fail_next(429, "throttled", "بیش از حد", retry_after=120)  # too long to wait
    with pytest.raises(RateLimitError) as info:
        client.get_payment(payment.authority)
    assert info.value.retry_after == 120


def test_503_retry_policy(gateway, client):
    payment = client.create_payment(10_000, "https://shop.test/back")
    gateway.pay(payment.authority)
    gateway.fail_next(503, "payment_busy", "در حال پردازش")
    assert client.verify_payment(payment.authority, 10_000).code == 100

    gateway.fail_next(503, "no_gateway_available", "درگاه فعالی نیست", times=5)
    with pytest.raises(ServiceUnavailableError) as info:
        client.create_payment(10_000, "https://shop.test/back")
    assert info.value.code == "no_gateway_available"
    assert len([r for r in gateway.requests if r[1] == "payments"]) == 2  # first create + one, no retry


def test_server_errors_are_retried_without_duplicates(gateway, client):
    gateway.fail_next(500, "server_error", "خطای داخلی")
    payment = client.create_payment(10_000, "https://shop.test/back", idempotency_key="order-500")
    creates = [r for r in gateway.requests if r[1] == "payments"]
    assert len(creates) == 2 and creates[0][2]["idempotency-key"] == creates[1][2]["idempotency-key"]
    assert len(gateway.payments) == 1 and payment.authority in gateway.payments

    gateway.fail_next(500, "server_error", "خطای داخلی", times=5)
    with pytest.raises(peyamesh.ServerError) as info:
        client.get_payment(payment.authority)
    assert info.value.status_code == 500


def test_no_retry_on_4xx(gateway, client):
    gateway.fail_next(400, "validation_error", "نامعتبر", times=3)
    with pytest.raises(InvalidRequestError):
        client.get_payment("A" * 36)
    assert len(gateway.requests) == 1


def test_non_json_error_body():
    transport = httpx.MockTransport(lambda r: httpx.Response(502, text="<html>Bad gateway</html>"))
    c = peyamesh.Peyamesh("k", http_client=httpx.Client(transport=transport), max_retries=0)
    with pytest.raises(peyamesh.ServerError) as info:
        c.get_payment("A" * 36)
    assert info.value.status_code == 502
    assert "Bad gateway" in info.value.body


def test_invalid_json_success_body():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
    c = peyamesh.Peyamesh("k", http_client=httpx.Client(transport=transport))
    with pytest.raises(peyamesh.APIError) as info:
        c.get_payment("A" * 36)
    assert info.value.code == "invalid_response"


def test_authority_is_path_escaped():
    seen = {}

    def handler(request):
        seen["path"] = request.url.raw_path.decode()
        return httpx.Response(404, json={"error": {"code": "payment_not_found", "message": "x"}})

    c = peyamesh.Peyamesh("k", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(NotFoundError):
        c.get_payment("../refunds/x")
    assert seen["path"] == "/api/v1/payments/..%2Frefunds%2Fx"


def test_request_body_is_json(gateway, client):
    client.create_payment(10_000, "https://shop.test/back", description="خرید")
    raw_headers = gateway.requests[-1][2]
    assert raw_headers["content-type"] == "application/json"
    assert json.dumps(gateway.requests[-1][3], ensure_ascii=False)


def test_full_refund_while_another_refund_is_in_progress():
    gateway = FakeGateway(auto_complete_refunds=False)
    client = gateway.client(currency="IRT")
    payment = client.create_payment(50_000, "https://shop.test/back")
    gateway.pay(payment.authority)
    client.verify_payment(payment.authority, 50_000)
    first = client.create_refund(payment.authority, amount=20_000)
    assert first.status == "approved"
    # No amount while `first` is in progress: the server refunds what is left.
    rest = client.create_refund(payment.authority, idempotency_key="rest-1")
    assert rest.amount_irr == 300_000
    sent = [r for r in gateway.requests if r[1].endswith("/refunds")]
    assert sent[-1][3] == {} and sent[-1][2]["idempotency-key"] == "rest-1"
    with pytest.raises(InvalidRequestError) as info:
        client.create_refund(payment.authority)  # nothing left at all
    assert info.value.code == "refund_amount_exceeds"
    assert info.value.details == {"refundable_irr": 0}
    gateway.complete_refund(first.id)
    gateway.complete_refund(rest.id)
    assert client.get_payment(payment.authority).status == PaymentStatus.REFUNDED
    # A replayed full refund is still recognised once the payment is fully refunded.
    assert client.create_refund(payment.authority, idempotency_key="rest-1").id == rest.id
    with pytest.raises(ConflictError):
        client.create_refund(payment.authority, amount=1_000, idempotency_key="rest-1")


def test_full_refund_falls_back_on_older_servers():
    gateway = FakeGateway(auto_complete_refunds=False)
    client = gateway.client(currency="IRT")
    payment = client.create_payment(50_000, "https://shop.test/back")
    gateway.pay(payment.authority)
    client.verify_payment(payment.authority, 50_000)
    client.create_refund(payment.authority, amount=20_000)
    # Older server versions reject an amount-less full refund while another refund is in progress.
    gateway.fail_next(
        400,
        "refund_amount_exceeds",
        "مبلغ بازپرداخت بیش از حد مجاز است.",
        path="/refunds",
        details={"refundable_irr": 300_000},
    )
    rest = client.create_refund(payment.authority, idempotency_key="rest-1")
    assert rest.amount_irr == 300_000
    sent = [r for r in gateway.requests if r[1].endswith("/refunds")]
    assert sent[-1][3] == {"amount": 300_000, "currency": "IRR"} and sent[-1][2]["idempotency-key"] == "rest-1"
