from __future__ import annotations

import datetime as dt
import threading
from io import StringIO
from urllib.parse import parse_qs, urlsplit

import pytest

django = pytest.importorskip("django")

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.core.checks import run_checks  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.dispatch import receiver  # noqa: E402
from django.test import override_settings  # noqa: E402
from django.utils import timezone  # noqa: E402

from peyamesh import APIConnectionError, ConfigurationError, InvalidRequestError  # noqa: E402
from peyamesh.contrib.django import (  # noqa: E402
    PeyameshPayment,
    PeyameshWebhookEvent,
    override_client,
    reconcile,
    refund_payment,
    settle_payment,
    start_payment,
)
from peyamesh.contrib.django.client import get_client  # noqa: E402
from peyamesh.contrib.django.signals import (  # noqa: E402
    payment_failed,
    payment_refunded,
    payment_verified,
    webhook_received,
)
from peyamesh.testing import FakeGateway  # noqa: E402

# Signals are sent on commit (like in production), so tests run with real transactions.
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("peyamesh.client.time.sleep", lambda s: None)


@pytest.fixture
def gateway():
    gw = FakeGateway()
    with override_client(gw.client(currency="IRT")):
        yield gw


@pytest.fixture
def events():
    seen: dict[str, list] = {"verified": [], "failed": [], "refunded": [], "webhook": []}

    def on_verified(sender, payment, **kw):
        seen["verified"].append(payment.pk)

    def on_failed(sender, payment, **kw):
        seen["failed"].append(payment.pk)

    def on_refunded(sender, payment, refund, **kw):
        seen["refunded"].append((payment.pk, refund.id if refund else None))

    def on_webhook(sender, event, payment, **kw):
        seen["webhook"].append((event.type, payment.pk if payment else None))

    pairs = [
        (payment_verified, on_verified),
        (payment_failed, on_failed),
        (payment_refunded, on_refunded),
        (webhook_received, on_webhook),
    ]
    for signal, fn in pairs:
        signal.connect(fn)
    yield seen
    for signal, fn in pairs:
        signal.disconnect(fn)


def _query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _start(client, amount=25_000, order="77"):
    response = client.post("/shop/checkout/", {"amount": str(amount), "order": order})
    assert response.status_code == 302, response.content
    record = PeyameshPayment.objects.get(order_id=order)
    assert response["Location"] == record.payment_url
    return record


def _return(client, gateway, record):
    url = gateway.callback_url(record.authority)
    parts = urlsplit(url)
    assert parts.path == "/peyamesh/callback/"
    return client.get(parts.path, dict(parse_qs(parts.query)))


# --------------------------------------------------------------------------- start
def test_start_payment_creates_pending_record(client, gateway):
    record = _start(client)
    assert record.status == PeyameshPayment.Status.PENDING
    assert record.authority in gateway.payments
    assert (record.amount, record.currency, record.amount_irr) == (25_000, "IRT", 250_000)
    assert record.callback_url == "http://testserver/peyamesh/callback/"
    sent = gateway.requests_to("payments", "POST")[-1]
    assert sent[2]["idempotency-key"] == f"dj-{record.public_id.hex}"
    assert sent[3]["order_id"] == "77"


def test_start_payment_failure_keeps_error_record(client, gateway):
    gateway.fail_next(400, "amount_too_low", "مبلغ کم است")
    response = client.post("/shop/checkout/", {"amount": "10", "order": "5"})
    assert response.status_code == 402
    assert response.content.decode() == "مبلغ کم است"
    record = PeyameshPayment.objects.get(order_id="5")
    assert record.status == "error" and record.failure_code == "amount_too_low"


def test_start_payment_without_request_needs_site_url(gateway):
    with pytest.raises(ConfigurationError):
        start_payment(amount=10_000, order_id="x")
    site = {**_settings(), "SITE_URL": "https://shop.test"}
    with override_settings(PEYAMESH=site), override_client(gateway.client(currency="IRT")):
        record = start_payment(amount=10_000, order_id="x")
    assert record.callback_url == "https://shop.test/peyamesh/callback/"


def test_start_payment_validates_amount(gateway):
    with pytest.raises(ValueError):
        start_payment(amount=0, callback_url="https://shop.test/cb")
    assert not PeyameshPayment.objects.exists()


def _settings():
    from django.conf import settings

    return dict(settings.PEYAMESH)


# --------------------------------------------------------------------------- callback
def test_successful_return_verifies_once(client, gateway, events):
    record = _start(client)
    gateway.pay(record.authority)
    response = _return(client, gateway, record)
    assert response.status_code == 302
    assert urlsplit(response["Location"]).path == "/shop/done/"
    assert _query(response["Location"]) == {"payment": str(record.public_id), "status": "success"}
    record.refresh_from_db()
    assert record.is_paid and record.ref_id and record.verified_at and record.card_pan
    assert events["verified"] == [record.pk]

    # the result page decides from the database
    assert client.get(response["Location"]).content == b"paid"

    # returning again (refresh / back button) is harmless and does not re-fire the signal
    again = _return(client, gateway, record)
    assert _query(again["Location"])["status"] == "success"
    assert events["verified"] == [record.pk]
    assert len(gateway.requests_to("payments/verify")) == 1


def test_forged_ok_status_is_not_trusted(client, gateway, events):
    record = _start(client)
    response = client.get("/peyamesh/callback/", {"Authority": record.authority, "Status": "OK"})
    assert _query(response["Location"])["status"] == "pending"
    record.refresh_from_db()
    assert record.status == "pending" and events["verified"] == []


def test_canceled_return(client, gateway, events):
    record = _start(client)
    gateway.fail(record.authority)
    response = _return(client, gateway, record)
    assert urlsplit(response["Location"]).path == "/shop/failed/"
    assert _query(response["Location"])["status"] == "failed"
    record.refresh_from_db()
    assert record.status == "failed" and record.failure_code == "canceled_by_user"
    assert events["failed"] == [record.pk]
    assert client.get(response["Location"]).content == b"not-paid"


def test_unknown_result_is_pending(client, gateway):
    record = _start(client)
    gateway.mark_unknown(record.authority)
    response = _return(client, gateway, record)
    assert _query(response["Location"])["status"] == "pending"
    record.refresh_from_db()
    assert record.status == "pending" and record.remote_status == "unknown"


def test_gateway_unreachable_on_return_is_pending(client, gateway):
    record = _start(client)
    gateway.pay(record.authority)
    gateway.network_error_next(times=5)
    response = _return(client, gateway, record)
    assert _query(response["Location"])["status"] == "pending"


@pytest.mark.parametrize("params", [{}, {"Authority": "A" + "9" * 35, "Status": "OK"}, {"Authority": "!!"}])
def test_invalid_or_unknown_callback(client, gateway, params):
    response = client.get("/peyamesh/callback/", params)
    assert response.status_code == 302
    assert urlsplit(response["Location"]).path == "/shop/failed/"
    assert _query(response["Location"]) == {"status": "failed"}


def test_per_payment_return_url(rf, gateway):
    request = rf.get("/")
    record = start_payment(request, amount=10_000, return_url="/orders/9/")
    gateway.pay(record.authority)
    from django.test import Client

    response = Client().get("/peyamesh/callback/", gateway.callback_params(record.authority))
    assert urlsplit(response["Location"]).path == "/orders/9/"


def test_amount_mismatch_marks_failed(client, gateway, events):
    record = _start(client)
    gateway.pay(record.authority)
    PeyameshPayment.objects.filter(pk=record.pk).update(amount=1)  # local tampering / bug
    response = _return(client, gateway, record)
    assert _query(response["Location"])["status"] == "failed"
    record.refresh_from_db()
    assert record.failure_code == "amount_mismatch" and events["verified"] == []


# --------------------------------------------------------------------------- webhooks
def _post_webhook(client, body, headers):
    return client.post(
        "/peyamesh/webhook/",
        data=body,
        content_type="application/json",
        **{"HTTP_" + k.upper().replace("-", "_"): v for k, v in headers.items() if k != "Content-Type"},
    )


def test_webhook_verifies_when_buyer_never_returns(client, gateway, events):
    record = _start(client)
    gateway.pay(record.authority)
    body, headers = gateway.webhook("payment.succeeded", record.authority)
    response = _post_webhook(client, body, headers)
    assert response.status_code == 200 and response.json() == {"received": True}
    record.refresh_from_db()
    assert record.is_paid
    assert events["verified"] == [record.pk]
    assert events["webhook"] == [("payment.succeeded", record.pk)]
    assert PeyameshWebhookEvent.objects.filter(event_id=headers["Peyamesh-Event-Id"]).exists()

    # duplicate delivery: ignored
    assert _post_webhook(client, body, headers).status_code == 200
    assert events["webhook"] == [("payment.succeeded", record.pk)]
    # buyer finally returns: no second fulfilment
    _return(client, gateway, record)
    assert events["verified"] == [record.pk]


def test_webhook_rejects_bad_signatures(client, gateway):
    record = _start(client)
    body, headers = gateway.webhook("payment.succeeded", record.authority, secret="whsec_wrong")
    response = _post_webhook(client, body, headers)
    assert response.status_code == 400 and response.json() == {"error": "invalid_signature"}
    body, headers = gateway.webhook("payment.succeeded", record.authority, timestamp=1)
    assert _post_webhook(client, body, headers).json() == {"error": "signature_expired"}
    assert _post_webhook(client, body, {}).json() == {"error": "missing_signature"}
    assert client.get("/peyamesh/webhook/").status_code == 405
    assert not PeyameshWebhookEvent.objects.exists()


def test_webhook_failed_and_expired(client, gateway, events):
    record = _start(client)
    gateway.expire(record.authority)
    body, headers = gateway.webhook("payment.expired", record.authority)
    assert _post_webhook(client, body, headers).status_code == 200
    record.refresh_from_db()
    assert record.status == "failed" and record.failure_code == "expired"
    assert events["failed"] == [record.pk]


def test_late_success_after_failure(client, gateway, events):
    record = _start(client)
    gateway.expire(record.authority)
    _post_webhook(client, *gateway.webhook("payment.expired", record.authority))
    gateway.pay(record.authority)  # the bank confirms late
    _post_webhook(client, *gateway.webhook("payment.succeeded", record.authority))
    record.refresh_from_db()
    assert record.is_paid
    assert events["failed"] == [record.pk] and events["verified"] == [record.pk]


def test_webhook_transient_error_asks_for_retry(client, gateway):
    record = _start(client)
    gateway.pay(record.authority)
    body, headers = gateway.webhook("payment.succeeded", record.authority)
    gateway.network_error_next(times=5)
    response = _post_webhook(client, body, headers)
    assert response.status_code == 503
    assert not PeyameshWebhookEvent.objects.exists()  # will be processed on redelivery
    assert _post_webhook(client, body, headers).status_code == 200
    record.refresh_from_db()
    assert record.is_paid


def test_webhook_for_unknown_payment_and_test_event(client, gateway, events):
    data = {"object": "payment", "authority": "A" + "0" * 35, "status": "paid", "amount": 10000, "currency": "IRT"}
    body, headers = gateway.webhook("payment.succeeded", data=data)
    assert _post_webhook(client, body, headers).status_code == 200
    assert events["webhook"] == [("payment.succeeded", None)]


def test_webhook_not_configured(client, gateway):
    with override_settings(PEYAMESH={**_settings(), "WEBHOOK_SECRET": None}):
        body, headers = gateway.webhook("payment.created", data={})
        assert _post_webhook(client, body, headers).status_code == 404


def test_webhook_secret_rotation(client, gateway):
    with override_settings(PEYAMESH={**_settings(), "WEBHOOK_SECRET": ["whsec_new", gateway.webhook_secret]}):
        body, headers = gateway.webhook("payment.created", data={})
        assert _post_webhook(client, body, headers).status_code == 200


# --------------------------------------------------------------------------- refunds
def test_refund_full_and_partial(client, gateway, events):
    record = _start(client, amount=10_000)
    gateway.pay(record.authority)
    _return(client, gateway, record)
    record.refresh_from_db()

    refund = refund_payment(record, amount=4_000, reason="یک قلم مرجوعی")
    assert refund.amount_irr == 40_000
    record.refresh_from_db()
    assert record.status == "partially_refunded" and record.refunded_irr == 40_000

    rest = refund_payment(record)
    assert rest.amount_irr == 60_000
    record.refresh_from_db()
    assert record.status == "refunded" and record.refundable_irr == 0
    assert [r for _, r in events["refunded"]] == [refund.id, rest.id]
    with pytest.raises(ValueError):
        refund_payment(record)


def test_refund_requires_verified_payment(client, gateway):
    record = _start(client)
    with pytest.raises(ValueError):
        refund_payment(record)


def test_refund_webhook_updates_record(client, gateway, events):
    record = _start(client, amount=10_000)
    gateway.pay(record.authority)
    _return(client, gateway, record)
    other = gateway.client()  # refund made elsewhere (dashboard)
    refund = other.create_refund(record.authority, amount=10_000)
    body, headers = gateway.webhook(
        "payment.refunded",
        record.authority,
        data={"object": "payment", **gateway._public(gateway.payments[record.authority]), "refund": refund.raw},
    )
    assert _post_webhook(client, body, headers).status_code == 200
    record.refresh_from_db()
    assert record.status == "refunded"
    assert events["refunded"] == [(record.pk, refund.id)]


# --------------------------------------------------------------------------- reconcile
def test_reconcile(client, gateway, events):
    paid = _start(client, order="1")
    canceled = _start(client, order="2")
    fresh = _start(client, order="3")
    gateway.pay(paid.authority)
    gateway.fail(canceled.authority)
    old = timezone.now() - dt.timedelta(hours=1)
    PeyameshPayment.objects.filter(pk__in=[paid.pk, canceled.pk]).update(created_at=old)

    out = StringIO()
    call_command("peyamesh_reconcile", stdout=out)
    assert out.getvalue().strip() == "checked=2 verified=1 failed=1 renotified=0 events_pruned=0 errors=0"
    assert PeyameshPayment.objects.get(pk=paid.pk).is_paid
    assert PeyameshPayment.objects.get(pk=canceled.pk).status == "failed"
    assert PeyameshPayment.objects.get(pk=fresh.pk).status == "pending"
    assert events["verified"] == [paid.pk]

    # paid-but-unverified payments are verified even when recent
    gateway.pay(fresh.authority)
    assert reconcile()["verified"] == 1
    assert PeyameshPayment.objects.get(pk=fresh.pk).is_paid


def test_reconcile_survives_gateway_outage(client, gateway):
    _start(client)
    PeyameshPayment.objects.update(created_at=timezone.now() - dt.timedelta(hours=1))
    gateway.network_error_next(times=20)
    stats = reconcile()
    assert stats["errors"] >= 1


# --------------------------------------------------------------------------- concurrency
def test_concurrent_settle_fires_once(gateway, events, rf):
    record = start_payment(rf.get("/"), amount=10_000)
    gateway.pay(record.authority)
    barrier = threading.Barrier(4)
    errors = []

    def worker():
        try:
            barrier.wait()
            settle_payment(PeyameshPayment.objects.get(pk=record.pk))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            from django.db import connection

            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert events["verified"] == [record.pk]


# --------------------------------------------------------------------------- misc
def test_signal_receiver_errors_do_not_break_the_return(client, gateway, caplog):
    @receiver(payment_verified)
    def broken(sender, payment, **kwargs):
        raise RuntimeError("fulfilment bug")

    try:
        record = _start(client)
        gateway.pay(record.authority)
        response = _return(client, gateway, record)
        assert _query(response["Location"])["status"] == "success"
        assert "fulfilment bug" in caplog.text
        record.refresh_from_db()
        assert record.is_paid and record.notified_at is None
        assert list(PeyameshPayment.objects.unnotified()) == [record]
    finally:
        payment_verified.disconnect(broken)


def test_failed_receivers_are_sent_again_until_they_succeed(client, gateway, events):
    calls = []

    @receiver(payment_verified)
    def flaky(sender, payment, **kwargs):  # fails twice (a bug, a database outage...), then works
        calls.append(payment.pk)
        if len(calls) <= 2:
            raise RuntimeError("temporary failure")

    try:
        record = _start(client)
        gateway.pay(record.authority)
        _return(client, gateway, record)
        assert PeyameshPayment.objects.get(pk=record.pk).notified_at is None

        assert reconcile()["renotified"] == 0  # too recent: the original call may still be running
        assert len(calls) == 1

        stats = reconcile(older_than_minutes=0)
        assert stats["renotified"] == 0 and stats["errors"] == 1
        stats = reconcile(older_than_minutes=0)
        assert stats["renotified"] == 1 and stats["errors"] == 0
        assert PeyameshPayment.objects.get(pk=record.pk).notified_at is not None
        assert reconcile(older_than_minutes=0)["renotified"] == 0
        assert calls == [record.pk] * 3
        assert events["verified"] == [record.pk] * 3  # every receiver gets it again: make them idempotent
        assert not PeyameshPayment.objects.unnotified().exists()
    finally:
        payment_verified.disconnect(flaky)


def test_successful_receivers_are_not_sent_again(client, gateway, events):
    record = _start(client)
    gateway.pay(record.authority)
    _return(client, gateway, record)
    assert PeyameshPayment.objects.get(pk=record.pk).notified_at is not None
    assert reconcile(older_than_minutes=0)["renotified"] == 0
    assert events["verified"] == [record.pk]


def test_receivers_are_sent_again_by_one_reconciler_only(client, gateway, events):
    fail = [True]

    @receiver(payment_verified)
    def flaky(sender, payment, **kwargs):
        if fail[0]:
            raise RuntimeError("temporary failure")

    try:
        record = _start(client)
        gateway.pay(record.authority)
        _return(client, gateway, record)
        fail[0] = False
        barrier = threading.Barrier(4)
        results = []

        def worker():
            try:
                barrier.wait()
                results.append(reconcile(older_than_minutes=0)["renotified"])
            finally:
                from django.db import connection

                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == [0, 0, 0, 1]
        assert events["verified"] == [record.pk, record.pk]
    finally:
        payment_verified.disconnect(flaky)


def test_receivers_are_not_retried_forever(client, gateway, events):
    @receiver(payment_verified)
    def broken(sender, payment, **kwargs):
        raise RuntimeError("fulfilment bug")

    try:
        record = _start(client)
        gateway.pay(record.authority)
        _return(client, gateway, record)
        PeyameshPayment.objects.filter(pk=record.pk).update(verified_at=timezone.now() - dt.timedelta(days=4))
        assert reconcile(older_than_minutes=0)["errors"] == 0
        assert events["verified"] == [record.pk]
    finally:
        payment_verified.disconnect(broken)


def test_old_webhook_events_are_pruned(client, gateway):
    record = _start(client)
    for _ in range(2):
        assert _post_webhook(client, *gateway.webhook("payment.pending", record.authority)).status_code == 200
    assert reconcile()["events_pruned"] == 0
    PeyameshWebhookEvent.objects.update(received_at=timezone.now() - dt.timedelta(days=31))
    assert reconcile()["events_pruned"] == 2
    assert not PeyameshWebhookEvent.objects.exists()

    keep_forever = {**settings.PEYAMESH, "WEBHOOK_EVENT_RETENTION_DAYS": None}
    with override_settings(PEYAMESH=keep_forever):
        assert _post_webhook(client, *gateway.webhook("payment.pending", record.authority)).status_code == 200
        PeyameshWebhookEvent.objects.update(received_at=timezone.now() - dt.timedelta(days=3650))
        assert reconcile()["events_pruned"] == 0
        assert PeyameshWebhookEvent.objects.count() == 1


def test_admin_pages(client, gateway):
    record = _start(client)
    user = get_user_model().objects.create_superuser("admin", "a@example.test", "pw")
    client.force_login(user)
    listing = client.get("/admin/peyamesh/peyameshpayment/")
    assert listing.status_code == 200 and record.order_id.encode() in listing.content
    assert client.get(f"/admin/peyamesh/peyameshpayment/{record.pk}/change/").status_code == 200
    assert client.get("/admin/peyamesh/peyameshpayment/add/").status_code == 403
    assert client.get("/admin/peyamesh/peyameshpayment/?q=77").status_code == 200

    gateway.pay(record.authority)
    response = client.post(
        "/admin/peyamesh/peyameshpayment/",
        {
            "action": "sync_with_gateway",
            "_selected_action": [record.pk],
        },
    )
    assert response.status_code == 302
    record.refresh_from_db()
    assert record.is_paid
    assert client.get("/admin/peyamesh/peyameshwebhookevent/").status_code == 200


def test_system_checks():
    with override_settings(PEYAMESH={"API_KEY": "k", "CURRENCY": "IRT", "WEBHOOK_SECRET": "s"}):
        assert [m.id for m in run_checks() if m.id and m.id.startswith("peyamesh")] == []
    with override_settings(PEYAMESH={"CURENCY": "IRT"}):
        ids = sorted(m.id for m in run_checks() if m.id and m.id.startswith("peyamesh"))
    assert ids == ["peyamesh.E002", "peyamesh.W001", "peyamesh.W002", "peyamesh.W003"]
    with override_settings(PEYAMESH={"API_KEY": "k", "CURRENCY": "USD", "WEBHOOK_SECRET": "s"}):
        assert "peyamesh.E003" in [m.id for m in run_checks()]
    for retention in (0, "30", 1.5):
        with override_settings(PEYAMESH={"API_KEY": "k", "WEBHOOK_EVENT_RETENTION_DAYS": retention}):
            assert "peyamesh.E005" in [m.id for m in run_checks()]


def test_system_check_for_missing_urls():
    conf = {"API_KEY": "k", "CURRENCY": "IRT", "WEBHOOK_SECRET": "s"}
    with override_settings(ROOT_URLCONF="tests.django_project.urls_bare", PEYAMESH=conf):
        assert "peyamesh.E004" in [m.id for m in run_checks()]
    with override_settings(
        ROOT_URLCONF="tests.django_project.urls_bare", PEYAMESH={**conf, "CALLBACK_URL": "https://shop.ir/pay/back"}
    ):
        assert "peyamesh.E004" not in [m.id for m in run_checks()]


def test_client_is_rebuilt_when_settings_change():
    first = get_client()
    assert get_client() is first
    with override_settings(PEYAMESH={**_settings(), "API_KEY": "other"}):
        assert get_client().api_key == "other"
    assert get_client().api_key == _settings()["API_KEY"]


def test_queryset_helpers(client, gateway):
    record = _start(client, order="42")
    assert list(PeyameshPayment.objects.for_order(42)) == [record]
    assert list(PeyameshPayment.objects.pending()) == [record]
    assert not PeyameshPayment.objects.paid().exists()
    assert "42" in str(record)


def test_validation_error_from_gateway_is_typed(gateway, rf):
    gateway.fail_next(400, "validation_error", "نامعتبر", details={"mobile": ["نامعتبر"]})
    with pytest.raises(InvalidRequestError) as info:
        start_payment(rf.get("/"), amount=10_000, mobile="123")
    assert info.value.details == {"mobile": ["نامعتبر"]}


def test_connection_error_on_start(gateway, rf):
    gateway.network_error_next(times=5)
    with pytest.raises(APIConnectionError):
        start_payment(rf.get("/"), amount=10_000)
    assert PeyameshPayment.objects.get().failure_code == "connection_error"
