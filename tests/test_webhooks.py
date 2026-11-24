from __future__ import annotations

import json
import time

import pytest

from peyamesh import EventType, SignatureVerificationError, webhooks
from peyamesh.testing import FakeGateway

SECRET = "whsec_abc"
BODY = json.dumps(
    {
        "id": "evt_1",
        "type": "payment.succeeded",
        "livemode": False,
        "created_at": "2026-11-24T10:00:00Z",
        "data": {"object": "payment", "authority": "A" + "1" * 35, "status": "paid", "amount": 1000, "currency": "IRT"},
    },
    separators=(",", ":"),
).encode()


def test_signature_matches_server_scheme():
    import hashlib
    import hmac

    ts = 1_700_000_000
    expected = hmac.new(SECRET.encode(), f"{ts}.".encode() + BODY, hashlib.sha256).hexdigest()
    assert webhooks.compute_signature(SECRET, ts, BODY) == expected
    assert webhooks.generate_test_header(BODY, SECRET, timestamp=ts) == f"t={ts},v1={expected}"


def test_construct_event():
    header = webhooks.generate_test_header(BODY, SECRET)
    event = webhooks.construct_event(BODY, header, SECRET)
    assert event.id == "evt_1" and event.type == EventType.PAYMENT_SUCCEEDED
    assert event.payment is not None and event.payment.amount == 1000
    assert event.refund is None
    assert event.created_at is not None and event.created_at.tzinfo is not None
    # str payload works too
    assert webhooks.construct_event(BODY.decode(), header, SECRET).id == "evt_1"


@pytest.mark.parametrize(
    ("header", "code"),
    [
        (None, "missing_signature"),
        ("", "missing_signature"),
        ("garbage", "invalid_signature_header"),
        ("t=abc,v1=00", "invalid_signature_header"),
        ("t=123", "invalid_signature_header"),
        ("v1=deadbeef", "invalid_signature_header"),
    ],
)
def test_malformed_headers(header, code):
    with pytest.raises(SignatureVerificationError) as info:
        webhooks.verify_signature(BODY, header, SECRET)
    assert info.value.code == code


def test_wrong_secret_and_tampered_body():
    header = webhooks.generate_test_header(BODY, SECRET)
    with pytest.raises(SignatureVerificationError) as info:
        webhooks.verify_signature(BODY, header, "whsec_other")
    assert info.value.code == "invalid_signature"
    with pytest.raises(SignatureVerificationError):
        webhooks.verify_signature(BODY.replace(b"1000", b"9000"), header, SECRET)


def test_timestamp_tolerance():
    old = int(time.time()) - 3600
    header = webhooks.generate_test_header(BODY, SECRET, timestamp=old)
    with pytest.raises(SignatureVerificationError) as info:
        webhooks.verify_signature(BODY, header, SECRET)
    assert info.value.code == "signature_expired"
    assert webhooks.verify_signature(BODY, header, SECRET, tolerance=None) == old
    assert webhooks.verify_signature(BODY, header, SECRET, tolerance=7200) == old


def test_secret_rotation_and_multiple_signatures():
    header = webhooks.generate_test_header(BODY, "whsec_new")
    assert webhooks.verify_signature(BODY, header, ["whsec_old", "whsec_new"])
    ts = int(time.time())
    multi = f"t={ts},v1=00,v1={webhooks.compute_signature(SECRET, ts, BODY)}"
    assert webhooks.verify_signature(BODY, multi, SECRET) == ts
    with pytest.raises(SignatureVerificationError) as info:
        webhooks.verify_signature(BODY, header, [])
    assert info.value.code == "webhook_secret_missing"


def test_invalid_payloads():
    for body in (b"not json", b"[]", b'{"id": "evt_1"}'):
        header = webhooks.generate_test_header(body, SECRET)
        with pytest.raises(SignatureVerificationError) as info:
            webhooks.construct_event(body, header, SECRET)
        assert info.value.code == "invalid_payload"
    huge = b"x" * (webhooks.MAX_PAYLOAD_BYTES + 1)
    with pytest.raises(SignatureVerificationError):
        webhooks.verify_signature(huge, webhooks.generate_test_header(huge, SECRET), SECRET)


def test_fake_gateway_webhook_roundtrip():
    gateway = FakeGateway()
    client = gateway.client()
    payment = client.create_payment(10_000, "https://shop.test/back")
    gateway.pay(payment.authority)
    body, headers = gateway.webhook("payment.succeeded", payment.authority)
    event = webhooks.construct_event(body, headers[webhooks.SIGNATURE_HEADER], gateway.webhook_secret)
    assert event.id == headers[webhooks.EVENT_ID_HEADER]
    assert event.payment is not None and event.payment.status == "paid"


def test_refund_event_payload():
    data = {
        "object": "payment",
        "authority": "A" * 36,
        "status": "refunded",
        "amount": 10,
        "currency": "IRT",
        "refund": {"object": "refund", "id": "rf_1", "authority": "A" * 36, "amount_irr": 100, "status": "succeeded"},
    }
    gateway = FakeGateway()
    body, headers = gateway.webhook("payment.refunded", data=data)
    event = webhooks.construct_event(body, headers["Peyamesh-Signature"], gateway.webhook_secret)
    assert event.refund is not None and event.refund.id == "rf_1"
    assert event.payment is not None and event.payment.status == "refunded"
