from __future__ import annotations

import datetime as dt
from urllib.parse import parse_qs

import pytest

from peyamesh import Currency, InvalidCallbackError, Payment, PaymentStatus, WebhookEvent, parse_callback
from peyamesh.contrib._flow import LocalStatus, local_status_for, outcome_for, refund_status_for, with_query
from peyamesh.models import parse_datetime

AUTH = "A0kq3w9xYb12345678901234567890abcde"


def test_parse_callback_variants():
    cb = parse_callback({"Authority": AUTH, "Status": "OK"})
    assert cb.authority == AUTH and cb.is_ok and not cb.is_pending
    cb = parse_callback(parse_qs(f"authority={AUTH}&status=nok&pm_status=unknown"))
    assert not cb.is_ok and cb.is_pending
    cb = parse_callback({"Authority": AUTH, "Status": "weird"})
    assert cb.status == "NOK"
    cb = parse_callback({"Authority": AUTH})
    assert cb.status == "NOK"


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"Authority": ""},
        {"Authority": "x"},
        {"Authority": "a b c d e f g h"},
        {"Authority": "<script>alert(1)</script>"},
        object(),
    ],
)
def test_parse_callback_rejects_bad_authority(params):
    with pytest.raises(InvalidCallbackError):
        parse_callback(params)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-24T10:00:00Z", dt.datetime(2026, 11, 24, 10, 0, tzinfo=dt.timezone.utc)),
        (
            "2026-11-24T13:30:00.5+03:30",
            dt.datetime(2026, 11, 24, 13, 30, 0, 500000, tzinfo=dt.timezone(dt.timedelta(hours=3, minutes=30))),
        ),
        ("2026-11-24T10:00:00.1234567+00:00", dt.datetime(2026, 11, 24, 10, 0, 0, 123456, tzinfo=dt.timezone.utc)),
        (None, None),
        ("", None),
        ("not a date", None),
    ],
)
def test_parse_datetime(raw, expected):
    assert parse_datetime(raw) == expected


def test_payment_lenient_parsing_and_flags():
    p = Payment.from_dict(
        {
            "authority": AUTH,
            "status": "paid",
            "amount": "100",
            "currency": "IRT",
            "new_field": 1,
            "ref_id": "123456789012",
            "metadata": None,
        }
    )
    assert p.amount == 100 and p.ref_id == 123456789012 and p.metadata == {}
    assert p.raw["new_field"] == 1
    assert p.is_paid and p.needs_verification and not p.is_verified
    assert p.status == PaymentStatus.PAID and p.currency == Currency.IRT
    assert str(PaymentStatus.PAID) == "paid"
    # Unexpected shapes never break parsing.
    assert Payment.from_dict({"authority": AUTH, "metadata": ["x"]}).metadata == {}
    assert WebhookEvent.from_dict({"id": "evt_1", "type": "payment.created", "data": "x"}).data == {}


def test_flow_rules():
    assert local_status_for("verified") == LocalStatus.VERIFIED
    assert local_status_for("paid") is None
    assert local_status_for("expired") == LocalStatus.FAILED
    assert local_status_for("unknown") == LocalStatus.PENDING
    assert local_status_for("something-new") == LocalStatus.PENDING
    assert outcome_for(LocalStatus.REFUNDED) == "success"
    assert outcome_for(LocalStatus.ERROR) == "failed"
    assert outcome_for(LocalStatus.PENDING) == "pending"
    assert refund_status_for(1000, 0) is None
    assert refund_status_for(1000, 400) == LocalStatus.PARTIALLY_REFUNDED
    assert refund_status_for(1000, 1000) == LocalStatus.REFUNDED


def test_with_query():
    assert with_query("/done/", {"payment": "x", "status": "success"}) == "/done/?payment=x&status=success"
    assert with_query("https://s.ir/a?b=1&status=old#f", {"status": "failed"}) == "https://s.ir/a?b=1&status=failed#f"
