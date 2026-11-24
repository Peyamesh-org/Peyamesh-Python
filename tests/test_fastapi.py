from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from peyamesh import ConfigurationError, PeyameshError
from peyamesh.contrib.fastapi import (
    MemoryStore,
    PaymentRecord,
    PaymentStore,
    PeyameshFastAPI,
    PeyameshSettings,
    SQLAlchemyStore,
)
from peyamesh.testing import FakeGateway


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr("peyamesh.async_client.anyio.sleep", instant)


_PG = os.environ.get("PEYAMESH_TEST_POSTGRES")  # e.g. postgres://user:pass@localhost:5432/db (needs asyncpg)


@pytest.fixture(params=["memory", "sqlalchemy", *(["postgres"] if _PG else [])])
def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryStore()
        return
    if request.param == "sqlalchemy":
        yield SQLAlchemyStore(f"sqlite+aiosqlite:///{tmp_path / 'shop.db'}")
        return
    url = _PG.replace("postgres://", "postgresql+asyncpg://", 1)
    prefix = f"pmtest_{uuid.uuid4().hex[:8]}_"
    yield SQLAlchemyStore(url, table_prefix=prefix)

    async def drop():
        cleanup = SQLAlchemyStore(url, table_prefix=prefix)
        async with cleanup.engine.begin() as conn:
            await conn.run_sync(cleanup.metadata.drop_all)
        await cleanup.close()

    asyncio.run(drop())


class Harness:
    def __init__(self, store, **settings):
        self.gateway = FakeGateway()
        self.events = {"verified": [], "failed": [], "refunded": [], "webhook": []}
        conf = PeyameshSettings(
            currency="IRT",
            webhook_secret=self.gateway.webhook_secret,
            success_url="/done",
            failure_url="/failed",
            **settings,
        )
        self.pm = PeyameshFastAPI(conf, store=store, client=self.gateway.async_client(currency="IRT"))
        self.store = store
        app = FastAPI(lifespan=self.pm.lifespan)
        app.include_router(self.pm.router, prefix="/peyamesh")

        @self.pm.on_payment_verified
        async def verified(payment):
            self.events["verified"].append(payment.id)

        @self.pm.on_payment_failed
        def failed(payment):  # sync hooks are supported too
            self.events["failed"].append(payment.id)

        @self.pm.on_payment_refunded
        async def refunded(payment, refund):
            self.events["refunded"].append((payment.id, refund.id if refund else None))

        @self.pm.on_webhook_event
        async def webhook(event, payment):
            self.events["webhook"].append((event.type, payment.id if payment else None))

        @app.post("/checkout/{order_id}")
        async def checkout(order_id: str, request: Request, amount: int = 25_000):
            try:
                payment = await self.pm.start_payment(request, amount=amount, order_id=order_id)
            except PeyameshError as exc:
                return PlainTextResponse(exc.message, status_code=402)
            return payment.redirect()

        @app.get("/done")
        @app.get("/failed")
        async def result(request: Request):
            payment = await self.pm.get_returned_payment(request)
            return PlainTextResponse("paid" if payment and payment.is_paid else "not-paid")

        self.app = app

    def __enter__(self):
        self.http = TestClient(self.app).__enter__()
        self.http.follow_redirects = False  # (constructor argument only exists in newer Starlette)
        return self

    def __exit__(self, *exc):
        self.http.__exit__(*exc)

    def run(self, fn, *args, **kwargs):
        async def call():
            return await fn(*args, **kwargs)

        return self.http.portal.call(call)

    def record(self, record_id) -> PaymentRecord:
        return self.run(self.store.get, record_id)

    def start(self, order="77", amount=25_000) -> PaymentRecord:
        response = self.http.post(f"/checkout/{order}", params={"amount": amount})
        assert response.status_code == 303, response.text
        record = self.run(self.store.get_by_authority, urlsplit(response.headers["location"]).path.rsplit("/", 1)[-1])
        assert record is not None and response.headers["location"] == record.payment_url
        return record

    def back(self, record):
        url = urlsplit(self.gateway.callback_url(record.authority))
        assert url.path == "/peyamesh/callback"
        return self.http.get(url.path + "?" + url.query)

    def webhook(self, body, headers):
        return self.http.post("/peyamesh/webhook", content=body, headers=headers)


def _q(response):
    return {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}


@pytest.fixture
def h(store):
    with Harness(store) as harness:
        yield harness


# --------------------------------------------------------------------------- flow
def test_store_implements_protocol(store):
    assert isinstance(store, PaymentStore)


def test_start_and_successful_return(h):
    record = h.start()
    assert record.status == "pending" and record.amount_irr == 250_000
    assert record.callback_url == "http://testserver/peyamesh/callback"
    sent = h.gateway.requests_to("payments", "POST")[-1]
    assert sent[2]["idempotency-key"] == f"fa-{record.id}" and sent[3]["order_id"] == "77"

    h.gateway.pay(record.authority)
    response = h.back(record)
    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == "/done"
    assert _q(response) == {"payment": record.id, "status": "success"}
    assert h.http.get(response.headers["location"]).text == "paid"

    stored = h.record(record.id)
    assert stored.is_paid and stored.ref_id and stored.card_pan and stored.verified_at
    assert stored.verified_at.tzinfo is not None
    assert h.events["verified"] == [record.id]

    h.back(record)  # refresh: no second verification, no second hook
    assert h.events["verified"] == [record.id]
    assert len(h.gateway.requests_to("payments/verify")) == 1


def test_forged_return_and_cancel(h):
    record = h.start()
    forged = h.http.get("/peyamesh/callback", params={"Authority": record.authority, "Status": "OK"})
    assert _q(forged)["status"] == "pending"
    h.gateway.fail(record.authority)
    response = h.back(record)
    assert urlsplit(response.headers["location"]).path == "/failed"
    assert _q(response)["status"] == "failed"
    stored = h.record(record.id)
    assert stored.status == "failed" and stored.failure_code == "canceled_by_user"
    assert h.events["failed"] == [record.id] and h.events["verified"] == []


def test_post_callback(h):
    record = h.start()
    h.gateway.pay(record.authority)
    response = h.http.post("/peyamesh/callback", data=h.gateway.callback_params(record.authority))
    assert _q(response)["status"] == "success"


def test_invalid_callbacks(h):
    for params in ({}, {"Authority": "A" + "9" * 35}, {"Authority": "<x>"}):
        response = h.http.get("/peyamesh/callback", params=params)
        assert response.status_code == 303 and _q(response) == {"status": "failed"}


def test_start_failure_keeps_error_record(h):
    h.gateway.fail_next(400, "amount_too_low", "مبلغ کم است")
    response = h.http.post("/checkout/5", params={"amount": 10})
    assert response.status_code == 402 and response.text == "مبلغ کم است"


def test_unknown_and_unreachable_are_pending(h):
    record = h.start()
    h.gateway.mark_unknown(record.authority)
    assert _q(h.back(record))["status"] == "pending"
    assert h.record(record.id).remote_status == "unknown"
    h.gateway.pay(record.authority)
    h.gateway.network_error_next(times=5)
    assert _q(h.back(record))["status"] == "pending"


# --------------------------------------------------------------------------- webhooks
def test_webhook_verifies_and_dedupes(h):
    record = h.start()
    h.gateway.pay(record.authority)
    body, headers = h.gateway.webhook("payment.succeeded", record.authority)
    response = h.webhook(body, headers)
    assert response.status_code == 200 and response.json() == {"received": True}
    assert h.record(record.id).is_paid
    assert h.webhook(body, headers).status_code == 200
    assert h.events["webhook"] == [("payment.succeeded", record.id)]
    h.back(record)
    assert h.events["verified"] == [record.id]


def test_webhook_signature_errors(h):
    record = h.start()
    body, headers = h.gateway.webhook("payment.succeeded", record.authority, secret="whsec_wrong")
    assert h.webhook(body, headers).json() == {"error": "invalid_signature"}
    body, headers = h.gateway.webhook("payment.succeeded", record.authority, timestamp=10)
    assert h.webhook(body, headers).json() == {"error": "signature_expired"}
    assert h.webhook(body, {}).json() == {"error": "missing_signature"}
    assert h.http.get("/peyamesh/webhook").status_code == 405


def test_webhook_failure_then_late_success(h):
    record = h.start()
    h.gateway.expire(record.authority)
    h.webhook(*h.gateway.webhook("payment.expired", record.authority))
    assert h.record(record.id).status == "failed"
    h.gateway.pay(record.authority)
    h.webhook(*h.gateway.webhook("payment.succeeded", record.authority))
    assert h.record(record.id).is_paid
    assert h.events["failed"] == [record.id] and h.events["verified"] == [record.id]


def test_webhook_transient_error_retries(h):
    record = h.start()
    h.gateway.pay(record.authority)
    body, headers = h.gateway.webhook("payment.succeeded", record.authority)
    h.gateway.network_error_next(times=5)
    assert h.webhook(body, headers).status_code == 503
    assert h.webhook(body, headers).status_code == 200
    assert h.record(record.id).is_paid


def test_webhook_not_configured(store):
    with Harness(store) as harness:
        harness.pm.settings.webhook_secret = None
        body, headers = harness.gateway.webhook("payment.created", data={})
        assert harness.webhook(body, headers).status_code == 404


# --------------------------------------------------------------------------- refunds & reconcile
def test_refunds(h):
    record = h.start(amount=10_000)
    h.gateway.pay(record.authority)
    h.back(record)
    record = h.record(record.id)
    first = h.run(h.pm.refund, record, amount=4_000)
    assert first.amount_irr == 40_000
    record = h.record(record.id)
    assert record.status == "partially_refunded" and record.refunded_irr == 40_000
    rest = h.run(h.pm.refund, record)
    assert rest.amount_irr == 60_000
    record = h.record(record.id)
    assert record.status == "refunded"
    assert [r for _, r in h.events["refunded"]] == [first.id, rest.id]
    with pytest.raises(ValueError):
        h.run(h.pm.refund, record)


def test_reconcile(h):
    paid = h.start(order="1")
    canceled = h.start(order="2")
    fresh = h.start(order="3")
    h.gateway.pay(paid.authority)
    h.gateway.fail(canceled.authority)
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    for r in (paid, canceled):
        h.run(h.store.update, r.id, {"created_at": old})
    stats = h.run(h.pm.reconcile)
    assert stats == {"checked": 2, "verified": 1, "failed": 1, "renotified": 0, "events_pruned": 0, "errors": 0}
    assert h.record(fresh.id).status == "pending"
    h.gateway.pay(fresh.authority)
    assert h.run(h.pm.reconcile)["verified"] == 1
    assert h.events["verified"] == [paid.id, fresh.id]


def test_concurrent_settle_fires_once(h):
    record = h.start()
    h.gateway.pay(record.authority)

    async def race():
        copies = [await h.store.get(record.id) for _ in range(5)]
        await asyncio.gather(*(h.pm.settle(c) for c in copies))

    h.run(race)
    assert h.events["verified"] == [record.id]


def test_hook_errors_are_contained(store, caplog):
    with Harness(store) as harness:

        @harness.pm.on_payment_verified
        async def broken(payment):
            raise RuntimeError("fulfilment bug")

        record = harness.start()
        harness.gateway.pay(record.authority)
        assert _q(harness.back(record))["status"] == "success"
        assert "fulfilment bug" in caplog.text
        assert harness.events["verified"] == [record.id]
        assert harness.record(record.id).notified_at is None


def test_failed_verified_hooks_run_again_until_they_succeed(store):
    with Harness(store) as h:
        calls = []

        @h.pm.on_payment_verified
        def flaky(payment):  # fails twice (a bug, a database outage...), then works
            calls.append(payment.id)
            if len(calls) <= 2:
                raise RuntimeError("temporary failure")

        record = h.start()
        h.gateway.pay(record.authority)
        assert _q(h.back(record))["status"] == "success"
        assert h.record(record.id).notified_at is None

        # Too recent: the original call may still be running.
        assert h.run(h.pm.reconcile)["renotified"] == 0
        assert len(calls) == 1

        stats = h.run(h.pm.reconcile, older_than_minutes=0)
        assert stats["renotified"] == 0 and stats["errors"] == 1
        stats = h.run(h.pm.reconcile, older_than_minutes=0)
        assert stats["renotified"] == 1 and stats["errors"] == 0
        assert h.record(record.id).notified_at is not None
        assert h.run(h.pm.reconcile, older_than_minutes=0)["renotified"] == 0
        assert calls == [record.id] * 3
        assert h.events["verified"] == [record.id] * 3  # every hook runs again, so make them idempotent


def test_successful_hooks_are_not_run_again(h):
    record = h.start()
    h.gateway.pay(record.authority)
    h.back(record)
    assert h.record(record.id).notified_at is not None
    assert h.run(h.pm.reconcile, older_than_minutes=0)["renotified"] == 0
    assert h.events["verified"] == [record.id]


def test_hooks_are_retried_by_one_reconciler_only(store):
    with Harness(store) as h:
        fail = [True]

        @h.pm.on_payment_verified
        async def flaky(payment):
            if fail[0]:
                raise RuntimeError("temporary failure")

        record = h.start()
        h.gateway.pay(record.authority)
        h.back(record)
        fail[0] = False

        async def race():
            return await asyncio.gather(*(h.pm.reconcile(older_than_minutes=0) for _ in range(4)))

        assert sum(s["renotified"] for s in h.run(race)) == 1
        assert h.events["verified"] == [record.id, record.id]


def test_hooks_are_not_retried_forever(store):
    with Harness(store) as h:

        @h.pm.on_payment_verified
        async def broken(payment):
            raise RuntimeError("fulfilment bug")

        record = h.start()
        h.gateway.pay(record.authority)
        h.back(record)
        long_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=4)
        h.run(h.store.update, record.id, {"verified_at": long_ago})
        assert h.run(h.pm.reconcile, older_than_minutes=0)["errors"] == 0
        assert h.events["verified"] == [record.id]


def test_old_webhook_events_are_pruned(h):
    record = h.start()
    for _ in range(2):
        assert h.webhook(*h.gateway.webhook("payment.pending", record.authority)).status_code == 200
    assert h.run(h.pm.reconcile)["events_pruned"] == 0

    async def age_events():
        if isinstance(h.store, MemoryStore):
            for event in h.store.events.values():
                event["received_at"] -= dt.timedelta(days=31)
            return
        async with h.store.engine.begin() as conn:
            old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=31)
            await conn.execute(h.store.events.update().values(received_at=old))

    h.run(age_events)
    assert h.run(h.pm.reconcile)["events_pruned"] == 2
    assert h.run(h.pm.reconcile)["events_pruned"] == 0


def test_event_retention_can_be_disabled(store):
    with Harness(store, webhook_event_retention_days=None) as h:
        assert h.run(h.pm.reconcile)["events_pruned"] == 0
    with pytest.raises(ValueError):
        PeyameshSettings(webhook_event_retention_days=0)


def test_payments_for_order(h):
    first = h.start(order="9")
    second = h.start(order="9")
    h.start(order="10")
    assert [r.id for r in h.run(h.pm.get_payments_for_order, 9)] == [second.id, first.id]
    assert h.run(h.pm.get_payments_for_order, "nope") == []


def test_oversized_webhook_is_rejected(h):
    body, headers = h.gateway.webhook("payment.pending", h.start().authority)
    big = body + b" " * (256 * 1024)
    assert h.webhook(big, headers).status_code == 413

    def chunks():
        yield big[: len(big) // 2]
        yield big[len(big) // 2 :]

    assert h.http.post("/peyamesh/webhook", content=chunks(), headers=headers).status_code == 413
    assert h.webhook(body, headers).status_code == 200


# --------------------------------------------------------------------------- configuration
def test_settings_from_env():
    conf = PeyameshSettings.from_env(
        environ={
            "PEYAMESH_API_KEY": "k",
            "PEYAMESH_CURRENCY": "irt",
            "PEYAMESH_WEBHOOK_SECRET": "a, b",
            "PEYAMESH_TIMEOUT": "5",
            "PEYAMESH_WEBHOOK_TOLERANCE": "off",
            "PEYAMESH_SUCCESS_URL": "/ok",
            "PEYAMESH_WEBHOOK_EVENT_RETENTION_DAYS": "7",
        },
        failure_url="/no",
    )
    assert conf.api_key == "k" and conf.currency == "IRT" and conf.webhook_secrets == ["a", "b"]
    assert conf.timeout == 5.0 and conf.webhook_tolerance is None
    assert (conf.success_url, conf.failure_url) == ("/ok", "/no")
    assert conf.webhook_event_retention_days == 7
    forever = PeyameshSettings.from_env(environ={"PEYAMESH_WEBHOOK_EVENT_RETENTION_DAYS": "none"})
    assert forever.webhook_event_retention_days is None
    with pytest.raises(ValueError):
        PeyameshSettings(currency="EUR")


def test_store_is_required_and_api_key_checked(monkeypatch):
    with pytest.raises(TypeError):
        PeyameshFastAPI(PeyameshSettings(api_key="k"))  # type: ignore[call-arg]
    monkeypatch.delenv("PEYAMESH_API_KEY", raising=False)
    pm = PeyameshFastAPI(PeyameshSettings(), store=MemoryStore())
    with pytest.raises(ConfigurationError):
        asyncio.run(pm.setup())


def test_start_without_request_needs_callback_url():
    gateway = FakeGateway()
    pm = PeyameshFastAPI(PeyameshSettings(), store=MemoryStore(), client=gateway.async_client())

    async def go():
        with pytest.raises(ConfigurationError):
            await pm.start_payment(amount=10_000)
        pm.settings.callback_url = "https://shop.test/peyamesh/callback"
        record = await pm.start_payment(amount=10_000, order_id=1)
        assert record.callback_url == "https://shop.test/peyamesh/callback"
        await pm.aclose()

    asyncio.run(go())


def test_sqlalchemy_store_rejects_sync_engine():
    sa = pytest.importorskip("sqlalchemy")
    with pytest.raises(TypeError):
        SQLAlchemyStore(sa.create_engine("sqlite://"))
