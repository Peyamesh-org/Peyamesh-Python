"""Plug-and-play FastAPI integration.

::

    from fastapi import FastAPI, Request
    from peyamesh.contrib.fastapi import PeyameshFastAPI, PeyameshSettings, SQLAlchemyStore

    peyamesh = PeyameshFastAPI(
        PeyameshSettings.from_env(success_url="/orders/done", failure_url="/orders/failed"),
        store=SQLAlchemyStore("sqlite+aiosqlite:///shop.db"),
    )
    app = FastAPI(lifespan=peyamesh.lifespan)
    app.include_router(peyamesh.router, prefix="/peyamesh")

    @peyamesh.on_payment_verified
    async def deliver(payment):          # idempotent: it runs again if it raised
        await orders.mark_paid(payment.order_id)

    @app.post("/checkout/{order_id}")
    async def checkout(order_id: str, request: Request):
        payment = await peyamesh.start_payment(request, amount=245_000, order_id=order_id)
        return payment.redirect()
"""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional, TypeVar, Union
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse, Response

from ..._base import Amount, _as_amount, _as_currency
from ...async_client import AsyncPeyamesh
from ...callback import InvalidCallbackError, parse_callback
from ...errors import (
    AmountMismatchError,
    ConfigurationError,
    NotFoundError,
    PaymentNotPaidError,
    PeyameshError,
    SignatureVerificationError,
)
from ...models import EventType, Payment, Refund, WebhookEvent
from ...webhooks import MAX_PAYLOAD_BYTES, SIGNATURE_HEADER, construct_event
from .._flow import (
    NOTIFY_RETRY_WINDOW,
    LocalStatus,
    Outcome,
    local_status_for,
    outcome_for,
    refund_status_for,
    remote_fields,
    with_query,
)
from .config import PeyameshSettings
from .records import PaymentRecord, utcnow
from .stores import PaymentStore

logger = logging.getLogger("peyamesh.fastapi")

Hook = Callable[..., Union[Any, Awaitable[Any]]]
H = TypeVar("H", bound=Hook)


class PeyameshFastAPI:
    """Payment flow for FastAPI: local records, callback + webhook endpoints, hooks and reconciliation.

    :param settings: see :class:`PeyameshSettings` (default: :meth:`PeyameshSettings.from_env`).
    :param store: where payment records live — :class:`SQLAlchemyStore` in production.
    :param client: an :class:`~peyamesh.AsyncPeyamesh` to use instead of one built from ``settings``.
    :param name: prefix of the route names (change it only to mount two integrations in one app).
    """

    def __init__(
        self,
        settings: Optional[PeyameshSettings] = None,
        *,
        store: PaymentStore,
        client: Optional[AsyncPeyamesh] = None,
        name: str = "peyamesh",
    ) -> None:
        if store is None:
            raise ConfigurationError("PeyameshFastAPI needs a store, e.g. store=SQLAlchemyStore(database_url).")
        self.settings = settings if settings is not None else PeyameshSettings.from_env()
        self.store = store
        self._client = client
        self._owns_client = client is None
        self._hooks: dict[str, list[Hook]] = defaultdict(list)
        self._ready = False
        self._setup_lock: Optional[asyncio.Lock] = None
        self.callback_route_name = f"{name}_callback"
        self.webhook_route_name = f"{name}_webhook"
        self.router = self._build_router()

    # ------------------------------------------------------------------ lifecycle
    @property
    def client(self) -> AsyncPeyamesh:
        if self._client is None:
            s = self.settings
            self._client = AsyncPeyamesh(
                s.api_key, base_url=s.base_url, timeout=s.timeout, max_retries=s.max_retries, currency=s.currency
            )
        return self._client

    async def setup(self) -> None:
        """Create tables (if the store does so) and validate the configuration. Idempotent."""
        if self._ready:
            return
        if self._setup_lock is None:  # created lazily, inside the running event loop
            self._setup_lock = asyncio.Lock()
        async with self._setup_lock:
            if not self._ready:
                _ = self.client  # fail fast on a missing API key
                await self.store.setup()
                if not self.settings.webhook_secrets:
                    logger.warning("peyamesh: webhook_secret is not set; webhooks will be rejected")
                self._ready = True

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
        await self.store.close()
        self._ready = False
        self._setup_lock = None

    @asynccontextmanager
    async def lifespan(self, app: Any = None) -> AsyncIterator[None]:
        """Use as ``FastAPI(lifespan=peyamesh.lifespan)`` or inside your own lifespan with ``async with``."""
        await self.setup()
        try:
            yield
        finally:
            await self.aclose()

    # ------------------------------------------------------------------ hooks
    def on_payment_verified(self, fn: H) -> H:
        """Register ``fn(payment)`` — called when a payment is verified. Deliver the order here.

        Called once; if any ``on_payment_verified`` hook raises (or the process dies before they ran),
        :meth:`reconcile` calls them all again until they all succeed, for up to 3 days. Make them idempotent.
        """
        self._hooks["verified"].append(fn)
        return fn

    def on_payment_failed(self, fn: H) -> H:
        """Register ``fn(payment)`` — called once when a payment fails, is canceled or expires."""
        self._hooks["failed"].append(fn)
        return fn

    def on_payment_refunded(self, fn: H) -> H:
        """Register ``fn(payment, refund)`` — called when money is refunded (``refund`` may be ``None``)."""
        self._hooks["refunded"].append(fn)
        return fn

    def on_webhook_event(self, fn: H) -> H:
        """Register ``fn(event, payment)`` — called for every authenticated webhook event (``payment`` may be
        ``None``)."""
        self._hooks["webhook"].append(fn)
        return fn

    async def _fire(self, hook: str, *args: Any) -> bool:
        """Run the hooks registered for ``hook``; errors are logged, never raised. Returns whether all succeeded."""
        ok = True
        for fn in list(self._hooks.get(hook, ())):
            try:
                if inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(type(fn).__call__):
                    await fn(*args)
                else:
                    result = await run_in_threadpool(fn, *args)
                    if inspect.isawaitable(result):
                        await result
            except Exception:
                ok = False
                logger.exception("peyamesh %s hook %r failed", hook, fn)
        return ok

    async def _notify_verified(self, record: PaymentRecord) -> bool:
        """Run the ``on_payment_verified`` hooks and remember success; on failure :meth:`reconcile` retries."""
        if not await self._fire("verified", record):
            logger.warning("peyamesh: on_payment_verified hooks for %s will run again on reconcile", record.id)
            return False
        now = utcnow()
        await self.store.mark_notified(record.id, now)
        record.notified_at = now
        return True

    # ------------------------------------------------------------------ store helpers
    async def _update(self, record: PaymentRecord, **values: Any) -> None:
        await self.store.update(record.id, values)
        for name, value in values.items():
            setattr(record, name, value)

    async def _transition(
        self,
        record: PaymentRecord,
        to_status: str,
        from_statuses: Any,
        *,
        refunded_below: Optional[int] = None,
        **values: Any,
    ) -> bool:
        won = await self.store.transition(record.id, to_status, from_statuses, values, refunded_below=refunded_below)
        fresh = await self.store.get(record.id)
        if fresh is not None:
            record.__dict__.update(fresh.__dict__)
        return won

    # ------------------------------------------------------------------ URLs
    def _absolute(self, url: str, request: Optional[Request]) -> str:
        if url.startswith(("http://", "https://")):
            return url
        if request is not None:
            return str(request.base_url).rstrip("/") + "/" + url.lstrip("/")
        raise ConfigurationError("Pass `request` to start_payment() or set an absolute `callback_url` setting.")

    def callback_url(self, request: Optional[Request] = None) -> str:
        if self.settings.callback_url:
            return self._absolute(self.settings.callback_url, request)
        if request is None:
            raise ConfigurationError(
                "Pass `request` to start_payment() or set the `callback_url` setting "
                "(e.g. 'https://shop.ir/peyamesh/callback')."
            )
        return str(request.url_for(self.callback_route_name))

    def return_url_for(self, record: Optional[PaymentRecord], outcome: str) -> str:
        s = self.settings
        if record is not None and record.return_url:
            base = record.return_url
        elif outcome == Outcome.SUCCESS:
            base = s.success_url
        elif outcome == Outcome.PENDING:
            base = s.pending_url or s.failure_url
        else:
            base = s.failure_url
        return with_query(base, {s.return_query_param: record.id if record is not None else None, "status": outcome})

    # ------------------------------------------------------------------ start
    async def start_payment(
        self,
        request: Optional[Request] = None,
        *,
        amount: Amount,
        order_id: Union[str, int, None] = None,
        description: str = "",
        mobile: str = "",
        email: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
        currency: Optional[str] = None,
        return_url: Optional[str] = None,
        callback_url: Optional[str] = None,
    ) -> PaymentRecord:
        """Register a payment and return its record; ``return record.redirect()`` sends the buyer to pay.

        :raises peyamesh.PeyameshError: the gateway refused the payment — show ``error.message`` to the buyer.
        """
        await self.setup()
        value = _as_amount(amount)
        unit = _as_currency(currency) or self.settings.currency or ""
        callback = self._absolute(callback_url, request) if callback_url else self.callback_url(request)
        record = PaymentRecord(
            amount=value,
            currency=unit,
            order_id="" if order_id is None else str(order_id)[:100],
            description=str(description)[:500],
            mobile=str(mobile)[:20],
            email=str(email)[:254],
            metadata=dict(metadata or {}),
            return_url=return_url or "",
            callback_url=callback,
        )
        await self.store.insert(record)
        try:
            payment = await self.client.create_payment(
                value,
                callback,
                currency=unit or None,
                description=record.description,
                order_id=record.order_id or None,
                mobile=record.mobile,
                email=record.email,
                metadata=record.metadata,
                idempotency_key=f"fa-{record.id}",
            )
        except PeyameshError as exc:
            await self._update(
                record, status=LocalStatus.ERROR, failure_code=exc.code[:64], failure_message=exc.message[:500]
            )
            logger.warning(
                "peyamesh payment %s could not be created: %s %s", record.id, exc, getattr(exc, "details", None) or ""
            )
            raise
        values = remote_fields(payment)
        values.update(
            authority=payment.authority,
            payment_url=payment.payment_url or "",
            currency=payment.currency or unit,
            status=local_status_for(payment.status) or LocalStatus.PENDING,
            last_synced_at=utcnow(),
        )
        await self._update(record, **values)
        return record

    # ------------------------------------------------------------------ lookups
    async def get_payment(self, record_id: str) -> Optional[PaymentRecord]:
        await self.setup()
        return await self.store.get(record_id)

    async def get_payment_by_authority(self, authority: str) -> Optional[PaymentRecord]:
        await self.setup()
        return await self.store.get_by_authority(authority)

    async def get_payments_for_order(self, order_id: Union[str, int]) -> list[PaymentRecord]:
        """Every payment of ``order_id``, newest first — e.g. to spot an order paid twice (two browser tabs)."""
        await self.setup()
        return await self.store.list_by_order(str(order_id))

    async def get_returned_payment(self, request: Request) -> Optional[PaymentRecord]:
        """On your success/failure page: the payment the buyer came back with. Decide from ``is_paid``."""
        raw = request.query_params.get(self.settings.return_query_param, "")
        if not raw or len(raw) > 64 or not raw.isalnum():
            return None
        return await self.get_payment(raw)

    # ------------------------------------------------------------------ settle
    async def _fail(self, record: PaymentRecord, code: str, message: str) -> None:
        if await self._transition(
            record,
            LocalStatus.FAILED,
            LocalStatus.FAILABLE,
            failure_code=code,
            failure_message=message[:500],
            last_synced_at=utcnow(),
        ):
            await self._fire("failed", record)

    async def _apply_not_paid(self, record: PaymentRecord, error: PaymentNotPaidError) -> None:
        remote: Optional[Payment] = None
        try:
            remote = await self.client.get_payment(record.authority or "")
        except PeyameshError:
            logger.info("peyamesh: could not fetch details of %s", record.authority, exc_info=True)
        status = remote.status if remote is not None else (error.payment_status or "")
        values: dict[str, Any] = remote_fields(remote) if remote is not None else {"remote_status": status}
        values["last_synced_at"] = utcnow()
        if local_status_for(status) == LocalStatus.FAILED:
            if await self._transition(record, LocalStatus.FAILED, LocalStatus.FAILABLE, **values):
                await self._fire("failed", record)
        elif record.status in (LocalStatus.NEW, LocalStatus.PENDING):
            await self._update(record, **values)

    async def settle(self, record: PaymentRecord) -> PaymentRecord:
        """Verify ``record`` with the gateway (using the amount stored locally). Idempotent and race-safe.

        :raises peyamesh.APIConnectionError: (and other transient errors) the outcome is unknown; retry later.
        """
        await self.setup()
        if not record.authority or record.status == LocalStatus.ERROR or record.status in LocalStatus.PAID:
            return record
        try:
            result = await self.client.verify_payment(record.authority, record.amount, currency=record.currency or None)
        except PaymentNotPaidError as exc:
            await self._apply_not_paid(record, exc)
            return record
        except AmountMismatchError as exc:
            logger.error("peyamesh: amount mismatch while verifying %s", record.authority)
            await self._fail(record, exc.code, exc.message)
            return record
        except NotFoundError as exc:
            logger.error("peyamesh: payment %s not found at the gateway (API key or mode changed?)", record.authority)
            await self._fail(record, "payment_not_found", exc.message)
            return record

        payment = result.payment
        values = remote_fields(payment)
        values.setdefault("verified_at", utcnow())
        values["last_synced_at"] = utcnow()
        target = local_status_for(payment.status) or LocalStatus.VERIFIED
        if target not in LocalStatus.PAID:
            target = LocalStatus.VERIFIED
        if await self._transition(record, target, LocalStatus.VERIFIABLE, **values):
            await self._notify_verified(record)
        return record

    # ------------------------------------------------------------------ callback
    async def process_callback(self, params: Any) -> tuple[Optional[PaymentRecord], str]:
        await self.setup()
        try:
            callback = parse_callback(params)
        except InvalidCallbackError:
            logger.warning("peyamesh: callback without a valid Authority")
            return None, Outcome.FAILED
        record = await self.store.get_by_authority(callback.authority)
        if record is None:
            logger.warning("peyamesh: callback for unknown authority %s", callback.authority)
            return None, Outcome.FAILED
        if record.status not in LocalStatus.PAID:
            try:
                await self.settle(record)
            except PeyameshError as exc:
                logger.warning("peyamesh: could not verify %s on return: %s", record.authority, exc)
                return record, Outcome.PENDING
        return record, outcome_for(record.status)

    # ------------------------------------------------------------------ webhooks
    async def _apply_refund(self, record: PaymentRecord, payment: Payment, refund: Optional[Refund]) -> None:
        target = refund_status_for(record.amount_irr or payment.amount_irr, payment.refunded_irr)
        if target is None:
            return
        if await self._transition(
            record,
            target,
            LocalStatus.PAID,
            refunded_below=payment.refunded_irr,
            refunded_irr=payment.refunded_irr,
            remote_status=payment.status,
            last_synced_at=utcnow(),
        ):
            await self._fire("refunded", record, refund)

    async def process_event(self, event: WebhookEvent) -> Optional[PaymentRecord]:
        """Apply an authenticated event; duplicates are ignored. Raises on transient failures."""
        await self.setup()
        if await self.store.has_event(event.id):
            return None
        payment, refund = event.payment, event.refund
        authority = payment.authority if payment is not None else (refund.authority if refund is not None else "")
        record = await self.store.get_by_authority(authority) if authority else None
        if record is not None and record.livemode is not None and record.livemode != event.livemode:
            logger.warning("peyamesh: ignoring %s event %s for a payment of the other mode", event.type, event.id)
            record = None

        if record is not None and payment is not None:
            if event.type == EventType.PAYMENT_SUCCEEDED:
                await self.settle(record)
            elif event.type in (EventType.PAYMENT_FAILED, EventType.PAYMENT_EXPIRED):
                values = remote_fields(payment)
                values["last_synced_at"] = utcnow()
                if await self._transition(record, LocalStatus.FAILED, LocalStatus.FAILABLE, **values):
                    await self._fire("failed", record)
            elif event.type == EventType.PAYMENT_REFUNDED:
                await self._apply_refund(record, payment, refund)
            elif record.status in (LocalStatus.NEW, LocalStatus.PENDING):
                await self._update(record, remote_status=payment.status, last_synced_at=utcnow())

        if await self.store.add_event(event.id, event.type, event.livemode, dict(event.raw)):
            await self._fire("webhook", event, record)
        return record

    # ------------------------------------------------------------------ refunds
    async def refund(
        self,
        record: PaymentRecord,
        *,
        amount: Optional[Amount] = None,
        currency: Optional[str] = None,
        reason: str = "",
        destination_iban: str = "",
        destination_card: str = "",
        idempotency_key: Optional[str] = None,
    ) -> Refund:
        """Refund ``amount`` (default: everything still refundable) of a verified payment.

        Pass ``idempotency_key`` (e.g. the id of your return request) to make repeated clicks safe.
        """
        await self.setup()
        if not record.is_paid or not record.authority:
            raise ValueError("Only verified payments can be refunded.")
        if record.refundable_irr <= 0:
            raise ValueError("Nothing left to refund on this payment.")
        # Full refund: let the gateway compute the refundable amount — it also accounts for refunds that are
        # still being processed, which the local record only learns about once they complete.
        value = None if amount is None else _as_amount(amount)
        unit = None if amount is None else (_as_currency(currency) or record.currency or "IRR")
        refund = await self.client.create_refund(
            record.authority,
            amount=value,
            currency=unit,
            reason=reason,
            destination_iban=destination_iban,
            destination_card=destination_card,
            idempotency_key=idempotency_key,
        )
        try:
            await self._apply_refund(record, await self.client.get_payment(record.authority), refund)
        except PeyameshError:
            logger.info("peyamesh: refund %s created; local sync deferred to webhook", refund.id, exc_info=True)
        return refund

    # ------------------------------------------------------------------ reconcile
    async def reconcile(
        self, *, older_than_minutes: Optional[int] = None, limit: Optional[int] = None
    ) -> dict[str, int]:
        """Catch up with the gateway. Run it every few minutes (:meth:`run_reconciler`, cron, a worker...).

        1. verifies every payment the gateway reports as paid-but-unverified (buyer closed the browser),
        2. re-checks local ``pending`` payments older than ``older_than_minutes``,
        3. runs the ``on_payment_verified`` hooks again for paid payments whose hooks raised (or never ran)
           more than ``older_than_minutes`` ago, for up to 3 days after verification, and
        4. forgets processed webhook events older than ``webhook_event_retention_days``.

        Returns counters: ``checked``, ``verified``, ``failed``, ``renotified``, ``events_pruned``, ``errors``.
        """
        await self.setup()
        s = self.settings
        minutes = s.reconcile_after_minutes if older_than_minutes is None else older_than_minutes
        batch = s.reconcile_limit if limit is None else limit
        stats = {"checked": 0, "verified": 0, "failed": 0, "renotified": 0, "events_pruned": 0, "errors": 0}

        async def run(record: PaymentRecord) -> None:
            stats["checked"] += 1
            try:
                await self.settle(record)
            except PeyameshError:
                stats["errors"] += 1
                logger.warning("peyamesh: reconcile could not settle %s", record.authority, exc_info=True)
                return
            if record.is_paid:
                stats["verified"] += 1
            elif record.is_failed:
                stats["failed"] += 1

        try:
            unverified = [p.authority for p in await self.client.list_unverified_payments()]
        except PeyameshError:
            stats["errors"] += 1
            logger.warning("peyamesh: reconcile could not list unverified payments", exc_info=True)
            unverified = []
        seen: set[str] = set()
        for record in await self.store.list_by_authorities(unverified):
            if record.status in LocalStatus.PAID or record.status == LocalStatus.ERROR:
                continue
            seen.add(record.id)
            await run(record)
        cutoff = utcnow() - dt.timedelta(minutes=minutes)
        stale = await self.store.list_stale(
            statuses=(LocalStatus.NEW, LocalStatus.PENDING), created_before=cutoff, limit=batch + len(seen)
        )
        for record in [r for r in stale if r.id not in seen][: max(0, batch)]:
            await run(record)

        now = utcnow()
        due = await self.store.list_unnotified(
            statuses=LocalStatus.PAID, updated_before=cutoff, verified_after=now - NOTIFY_RETRY_WINDOW, limit=batch
        )
        for record in due:
            # Claim the record first, so that concurrent reconcilers never run the hooks twice.
            if await self.store.claim(record.id, updated_before=cutoff):
                if await self._notify_verified(record):
                    stats["renotified"] += 1
                else:
                    stats["errors"] += 1

        if s.webhook_event_retention_days is not None:
            expired = now - dt.timedelta(days=s.webhook_event_retention_days)
            stats["events_pruned"] = await self.store.prune_events(received_before=expired)
        return stats

    async def run_reconciler(self, interval: float = 300.0) -> None:
        """Run :meth:`reconcile` forever, every ``interval`` seconds. Start it as a background task::

        @asynccontextmanager
        async def lifespan(app):
            async with peyamesh.lifespan(app):
                task = asyncio.create_task(peyamesh.run_reconciler())
                yield
                task.cancel()
        """
        while True:
            try:
                stats = await self.reconcile()
                if stats["checked"]:
                    logger.info("peyamesh reconcile: %s", stats)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("peyamesh reconcile failed")
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------ endpoints
    async def handle_callback(self, request: Request) -> Response:
        params: dict[str, Any] = dict(request.query_params)
        if request.method == "POST" and "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
            body = await request.body()
            params.update(parse_qsl(body.decode("utf-8", "replace")[:8192], keep_blank_values=False))
        record, outcome = await self.process_callback(params)
        return RedirectResponse(
            self.return_url_for(record, outcome), status_code=303, headers={"Cache-Control": "no-store"}
        )

    async def handle_webhook(self, request: Request) -> Response:
        secrets = self.settings.webhook_secrets
        if not secrets:
            logger.error("peyamesh: webhook received but webhook_secret is not configured")
            return JSONResponse({"error": "webhook_not_configured"}, status_code=404)
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_PAYLOAD_BYTES:
            return JSONResponse({"error": "payload_too_large"}, status_code=413)
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_PAYLOAD_BYTES:  # chunked bodies have no Content-Length
                return JSONResponse({"error": "payload_too_large"}, status_code=413)
        try:
            event = construct_event(
                body, request.headers.get(SIGNATURE_HEADER), secrets, tolerance=self.settings.webhook_tolerance
            )
        except SignatureVerificationError as exc:
            logger.warning("peyamesh: rejected webhook (%s)", exc.code)
            return JSONResponse({"error": exc.code}, status_code=400)
        try:
            await self.process_event(event)
        except PeyameshError as exc:
            logger.warning("peyamesh: webhook %s will be retried: %s", event.id, exc)
            return JSONResponse({"error": "retry_later"}, status_code=503)
        return JSONResponse({"received": True})

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        async def callback(request: Request) -> Response:
            return await self.handle_callback(request)

        async def webhook(request: Request) -> Response:
            return await self.handle_webhook(request)

        router.add_api_route(
            "/callback", callback, methods=["GET", "POST"], name=self.callback_route_name, include_in_schema=False
        )
        router.add_api_route(
            "/webhook", webhook, methods=["POST"], name=self.webhook_route_name, include_in_schema=False
        )
        return router


__all__ = ["PeyameshFastAPI"]
