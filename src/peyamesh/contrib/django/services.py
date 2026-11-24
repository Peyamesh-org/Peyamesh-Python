"""Payment flow for Django: start → buyer pays → callback / webhook / reconcile → verified.

Public functions (also importable from :mod:`peyamesh.contrib.django`):

* :func:`start_payment` — register a payment and get the record to redirect the buyer with.
* :func:`get_returned_payment` — on your success/failure page, load the payment the buyer came back with.
* :func:`settle_payment` — verify / sync one payment with the gateway (idempotent).
* :func:`refund_payment` — refund all or part of a verified payment.
* :func:`reconcile` — catch up on payments whose buyer never came back (run periodically).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Mapping
from typing import Any, Optional, Union

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.dispatch import Signal
from django.http import HttpRequest
from django.shortcuts import resolve_url
from django.urls import reverse
from django.utils import timezone

from ..._base import Amount, _as_amount, _as_currency
from ...callback import InvalidCallbackError, parse_callback
from ...errors import (
    AmountMismatchError,
    ConfigurationError,
    NotFoundError,
    PaymentNotPaidError,
    PeyameshError,
)
from ...models import EventType, Payment, Refund, WebhookEvent
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
from .client import get_client
from .conf import get_settings
from .models import PeyameshPayment, PeyameshWebhookEvent
from .signals import payment_failed, payment_refunded, payment_verified, webhook_received

logger = logging.getLogger("peyamesh.django")


# --------------------------------------------------------------------------- internals
def _send(signal: Signal, **kwargs: Any) -> bool:
    """Send ``signal`` now; receiver errors are logged, never raised. Returns whether every receiver succeeded."""
    ok = True
    for receiver, result in signal.send_robust(sender=PeyameshPayment, **kwargs):
        if isinstance(result, Exception):
            ok = False
            logger.error(
                "peyamesh signal receiver %r failed",
                receiver,
                exc_info=(type(result), result, result.__traceback__),
            )
    return ok


def _emit(signal: Signal, **kwargs: Any) -> None:
    """Send ``signal`` after the current transaction commits."""
    transaction.on_commit(lambda: _send(signal, **kwargs))


def _notify_verified(record: PeyameshPayment) -> bool:
    """Send ``payment_verified`` and remember success; on failure ``reconcile`` sends it again."""
    if not _send(payment_verified, payment=record):
        logger.warning("peyamesh: payment_verified for %s will be sent again by reconcile", record.public_id)
        return False
    now = timezone.now()
    PeyameshPayment.objects.filter(pk=record.pk, notified_at=None).update(notified_at=now)
    record.notified_at = now
    return True


def _emit_verified(record: PeyameshPayment) -> None:
    transaction.on_commit(lambda: _notify_verified(record))


def _update(record: PeyameshPayment, **fields: Any) -> None:
    fields["updated_at"] = timezone.now()
    PeyameshPayment.objects.filter(pk=record.pk).update(**fields)
    for name, value in fields.items():
        setattr(record, name, value)


def _transition(record: PeyameshPayment, to_status: str, from_statuses: Any, **fields: Any) -> bool:
    """Atomically move ``record`` to ``to_status`` if it is still in one of ``from_statuses``."""
    fields.update(status=to_status, updated_at=timezone.now())
    won = PeyameshPayment.objects.filter(pk=record.pk, status__in=list(from_statuses)).update(**fields) == 1
    if won:
        for name, value in fields.items():
            setattr(record, name, value)
    else:
        record.refresh_from_db()
    return won


def _absolute(url: str, request: Optional[HttpRequest]) -> str:
    if url.startswith(("http://", "https://")):
        return url
    if request is not None:
        return request.build_absolute_uri(url)
    site_url = get_settings().site_url
    if site_url:
        return site_url.rstrip("/") + "/" + url.lstrip("/")
    raise ConfigurationError(
        "Cannot build an absolute callback URL: pass `request` to start_payment() or set "
        "PEYAMESH['SITE_URL'] (e.g. 'https://shop.ir') or PEYAMESH['CALLBACK_URL']."
    )


def build_callback_url(request: Optional[HttpRequest] = None) -> str:
    """Absolute URL of the integration's callback view (``PEYAMESH['CALLBACK_URL']`` wins if set)."""
    conf = get_settings()
    return _absolute(resolve_url(conf.callback_url) if conf.callback_url else reverse("peyamesh:callback"), request)


# --------------------------------------------------------------------------- start
def start_payment(
    request: Optional[HttpRequest] = None,
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
) -> PeyameshPayment:
    """Register a payment at the gateway and return its local record.

    ::

        payment = start_payment(request, amount=order.total, order_id=order.pk, mobile=order.phone)
        return payment.redirect()

    ``amount`` is in ``currency`` (default ``PEYAMESH['CURRENCY']``). ``return_url`` (optional) is where the
    buyer lands afterwards, instead of ``SUCCESS_URL`` / ``FAILURE_URL``; the integration appends
    ``?payment=<id>&status=success|failed|pending``.

    :raises peyamesh.PeyameshError: the gateway refused the payment — show ``error.message`` to the buyer.
       The record is kept with status ``error`` for your records.
    """
    conf = get_settings()
    value = _as_amount(amount)
    unit = _as_currency(currency) or conf.currency or ""
    callback = _absolute(callback_url, request) if callback_url else build_callback_url(request)
    record = PeyameshPayment.objects.create(
        amount=value,
        currency=unit,
        order_id="" if order_id is None else str(order_id)[:100],
        description=str(description)[:500],
        mobile=str(mobile)[:20],
        email=str(email)[:254],
        metadata=dict(metadata or {}),
        return_url=resolve_url(return_url) if return_url else "",
        callback_url=callback,
        status=LocalStatus.NEW,
    )
    try:
        payment = get_client().create_payment(
            value,
            callback,
            currency=unit or None,
            description=record.description,
            order_id=record.order_id or None,
            mobile=record.mobile,
            email=record.email,
            metadata=record.metadata,
            idempotency_key=f"dj-{record.public_id.hex}",
        )
    except PeyameshError as exc:
        _update(record, status=LocalStatus.ERROR, failure_code=exc.code[:64], failure_message=exc.message[:500])
        logger.warning(
            "peyamesh payment %s could not be created: %s %s",
            record.public_id,
            exc,
            getattr(exc, "details", None) or "",
        )
        raise
    fields = remote_fields(payment)
    fields.update(
        authority=payment.authority,
        payment_url=payment.payment_url or "",
        currency=payment.currency or unit,
        status=local_status_for(payment.status) or LocalStatus.PENDING,
        last_synced_at=timezone.now(),
    )
    _update(record, **fields)
    return record


# --------------------------------------------------------------------------- settle
def _apply_not_paid(record: PeyameshPayment, error: PaymentNotPaidError) -> None:
    remote: Optional[Payment] = None
    try:
        remote = get_client().get_payment(record.authority or "")
    except PeyameshError:
        logger.info("peyamesh: could not fetch details of %s", record.authority, exc_info=True)
    status = remote.status if remote is not None else (error.payment_status or "")
    fields: dict[str, Any] = remote_fields(remote) if remote is not None else {"remote_status": status}
    fields["last_synced_at"] = timezone.now()
    if local_status_for(status) == LocalStatus.FAILED:
        if _transition(record, LocalStatus.FAILED, LocalStatus.FAILABLE, **fields):
            _emit(payment_failed, payment=record)
    elif record.status in (LocalStatus.NEW, LocalStatus.PENDING):
        _update(record, **fields)


def _fail(record: PeyameshPayment, code: str, message: str) -> None:
    if _transition(
        record,
        LocalStatus.FAILED,
        LocalStatus.FAILABLE,
        failure_code=code,
        failure_message=message[:500],
        last_synced_at=timezone.now(),
    ):
        _emit(payment_failed, payment=record)


def settle_payment(record: PeyameshPayment) -> PeyameshPayment:
    """Verify ``record`` with the gateway and update it. Safe to call any number of times, concurrently.

    Verification always uses the amount stored in the record — never anything sent by the buyer.

    :raises peyamesh.APIConnectionError: (and other transient API errors) the result is unknown; retry later.
    """
    if not record.authority or record.status == LocalStatus.ERROR or record.status in LocalStatus.PAID:
        return record
    try:
        result = get_client().verify_payment(record.authority, record.amount, currency=record.currency or None)
    except PaymentNotPaidError as exc:
        _apply_not_paid(record, exc)
        return record
    except AmountMismatchError as exc:
        logger.error(
            "peyamesh: amount mismatch while verifying %s (local amount %s %s)",
            record.authority,
            record.amount,
            record.currency,
        )
        _fail(record, exc.code, exc.message)
        return record
    except NotFoundError as exc:
        logger.error("peyamesh: payment %s not found at the gateway (API key or mode changed?)", record.authority)
        _fail(record, "payment_not_found", exc.message)
        return record

    payment = result.payment
    fields = remote_fields(payment)
    fields.setdefault("verified_at", timezone.now())
    fields["last_synced_at"] = timezone.now()
    target = local_status_for(payment.status) or LocalStatus.VERIFIED
    if target not in LocalStatus.PAID:  # defensive: verify succeeded, the payment is verified
        target = LocalStatus.VERIFIED
    if _transition(record, target, LocalStatus.VERIFIABLE, **fields):
        _emit_verified(record)
    return record


# --------------------------------------------------------------------------- callback
def process_callback(params: Any) -> tuple[Optional[PeyameshPayment], str]:
    """Handle the buyer's return. Returns the record (``None`` if unknown) and an :class:`Outcome`."""
    try:
        callback = parse_callback(params)
    except InvalidCallbackError:
        logger.warning("peyamesh: callback without a valid Authority")
        return None, Outcome.FAILED
    record = PeyameshPayment.objects.filter(authority=callback.authority).first()
    if record is None:
        logger.warning("peyamesh: callback for unknown authority %s", callback.authority)
        return None, Outcome.FAILED
    if record.status not in LocalStatus.PAID:
        try:
            settle_payment(record)
        except PeyameshError as exc:
            logger.warning("peyamesh: could not verify %s on return: %s", record.authority, exc)
            return record, Outcome.PENDING
    return record, outcome_for(record.status)


def return_url_for(record: Optional[PeyameshPayment], outcome: str) -> str:
    conf = get_settings()
    if record is not None and record.return_url:
        base = record.return_url
    elif outcome == Outcome.SUCCESS:
        base = conf.success_url
    elif outcome == Outcome.PENDING:
        base = conf.pending_url or conf.failure_url
    else:
        base = conf.failure_url
    params = {conf.return_query_param: str(record.public_id) if record is not None else None, "status": outcome}
    return with_query(resolve_url(base), params)


def get_returned_payment(request: HttpRequest) -> Optional[PeyameshPayment]:
    """On your ``SUCCESS_URL`` / ``FAILURE_URL`` view, load the payment the buyer returned with.

    Always decide from ``payment.is_paid`` — never from the ``status`` query parameter.
    """
    raw = request.GET.get(get_settings().return_query_param, "")
    try:
        public_id = uuid.UUID(str(raw))
    except ValueError:
        return None
    return PeyameshPayment.objects.filter(public_id=public_id).first()


# --------------------------------------------------------------------------- webhooks
def _apply_refund(record: PeyameshPayment, payment: Payment, refund: Optional[Refund]) -> None:
    target = refund_status_for(record.amount_irr or payment.amount_irr, payment.refunded_irr)
    if target is None:
        return
    updated = PeyameshPayment.objects.filter(
        pk=record.pk, status__in=list(LocalStatus.PAID), refunded_irr__lt=payment.refunded_irr
    ).update(
        status=target,
        refunded_irr=payment.refunded_irr,
        remote_status=payment.status,
        last_synced_at=timezone.now(),
        updated_at=timezone.now(),
    )
    record.refresh_from_db()
    if updated:
        _emit(payment_refunded, payment=record, refund=refund)


def process_event(event: WebhookEvent) -> Optional[PeyameshPayment]:
    """Apply an authenticated webhook event. Duplicate deliveries are ignored.

    :raises peyamesh.PeyameshError: transient failure — answer non-2xx so the event is delivered again.
    """
    if PeyameshWebhookEvent.objects.filter(event_id=event.id).exists():
        return None
    payment = event.payment
    refund = event.refund
    authority = payment.authority if payment is not None else (refund.authority if refund is not None else "")
    record = PeyameshPayment.objects.filter(authority=authority).first() if authority else None
    if record is not None and record.livemode is not None and record.livemode != event.livemode:
        logger.warning("peyamesh: ignoring %s event %s for a payment of the other mode", event.type, event.id)
        record = None

    if record is not None and payment is not None:
        if event.type == EventType.PAYMENT_SUCCEEDED:
            settle_payment(record)
        elif event.type in (EventType.PAYMENT_FAILED, EventType.PAYMENT_EXPIRED):
            fields = remote_fields(payment)
            fields["last_synced_at"] = timezone.now()
            if _transition(record, LocalStatus.FAILED, LocalStatus.FAILABLE, **fields):
                _emit(payment_failed, payment=record)
        elif event.type == EventType.PAYMENT_REFUNDED:
            _apply_refund(record, payment, refund)
        elif record.status in (LocalStatus.NEW, LocalStatus.PENDING):
            _update(record, remote_status=payment.status, last_synced_at=timezone.now())

    try:
        with transaction.atomic():
            PeyameshWebhookEvent.objects.create(
                event_id=event.id, type=event.type[:64], livemode=event.livemode, payload=dict(event.raw)
            )
    except IntegrityError:  # a concurrent delivery of the same event got there first
        return record
    _emit(webhook_received, event=event, payment=record)
    return record


# --------------------------------------------------------------------------- refunds
def refund_payment(
    record: PeyameshPayment,
    *,
    amount: Optional[Amount] = None,
    currency: Optional[str] = None,
    reason: str = "",
    destination_iban: str = "",
    destination_card: str = "",
    idempotency_key: Optional[str] = None,
) -> Refund:
    """Refund ``amount`` (in ``currency``, default the payment currency) or everything still refundable.

    The local record is updated when the refund completes (immediately, or later through the
    ``payment.refunded`` webhook when refunds need approval). Pass ``idempotency_key`` (for example the id
    of your return request) to make a repeated click or a re-run safe; network retries are always safe.
    """
    if not record.is_paid or not record.authority:
        raise ValueError("Only verified payments can be refunded.")
    if record.refundable_irr <= 0:
        raise ValueError("Nothing left to refund on this payment.")
    # Full refund: let the gateway compute the refundable amount — it also accounts for refunds that are
    # still being processed, which the local record only learns about once they complete.
    value = None if amount is None else _as_amount(amount)
    unit = None if amount is None else (_as_currency(currency) or record.currency or "IRR")
    refund = get_client().create_refund(
        record.authority,
        amount=value,
        currency=unit,
        reason=reason,
        destination_iban=destination_iban,
        destination_card=destination_card,
        idempotency_key=idempotency_key,
    )
    try:
        _apply_refund(record, get_client().get_payment(record.authority), refund)
    except PeyameshError:
        logger.info("peyamesh: refund %s created; local sync deferred to webhook", refund.id, exc_info=True)
    return refund


# --------------------------------------------------------------------------- reconcile
def reconcile(*, older_than_minutes: Optional[int] = None, limit: Optional[int] = None) -> dict[str, int]:
    """Bring local records up to date with the gateway. Run it every few minutes (cron, Celery beat, ...).

    1. verifies every payment the gateway reports as paid-but-unverified (buyer closed the browser),
    2. re-checks local ``pending`` payments older than ``older_than_minutes``,
    3. sends ``payment_verified`` again for paid payments whose receivers raised (or never ran) more than
       ``older_than_minutes`` ago, for up to 3 days after verification, and
    4. deletes processed webhook events older than ``WEBHOOK_EVENT_RETENTION_DAYS``.

    Returns counters: ``checked``, ``verified``, ``failed``, ``renotified``, ``events_pruned``, ``errors``.
    """
    conf = get_settings()
    minutes = conf.reconcile_after_minutes if older_than_minutes is None else older_than_minutes
    batch = conf.reconcile_limit if limit is None else limit
    stats = {"checked": 0, "verified": 0, "failed": 0, "renotified": 0, "events_pruned": 0, "errors": 0}

    def run(record: PeyameshPayment) -> None:
        stats["checked"] += 1
        try:
            settle_payment(record)
        except PeyameshError:
            stats["errors"] += 1
            logger.warning("peyamesh: reconcile could not settle %s", record.authority, exc_info=True)
            return
        if record.is_paid:
            stats["verified"] += 1
        elif record.is_failed:
            stats["failed"] += 1

    try:
        unverified = [p.authority for p in get_client().list_unverified_payments()]
    except PeyameshError:
        stats["errors"] += 1
        logger.warning("peyamesh: reconcile could not list unverified payments", exc_info=True)
        unverified = []
    seen: set[int] = set()
    if unverified:
        for record in PeyameshPayment.objects.filter(authority__in=unverified).exclude(
            status__in=[*LocalStatus.PAID, LocalStatus.ERROR]
        ):
            seen.add(record.pk)
            run(record)

    cutoff = timezone.now() - dt.timedelta(minutes=minutes)
    stale = (
        PeyameshPayment.objects.filter(status__in=(LocalStatus.NEW, LocalStatus.PENDING), created_at__lte=cutoff)
        .filter(~Q(authority=None))
        .exclude(pk__in=seen)
        .order_by(F("last_synced_at").asc(nulls_first=True), "created_at")[: max(0, batch)]
    )
    for record in stale:
        run(record)

    now = timezone.now()
    due = (
        PeyameshPayment.objects.unnotified()
        .filter(updated_at__lte=cutoff, verified_at__gte=now - NOTIFY_RETRY_WINDOW)
        .order_by("updated_at")[: max(0, batch)]
    )
    for record in due:
        # Claim the record first, so that concurrent reconcilers never send the signal twice.
        claim = PeyameshPayment.objects.filter(pk=record.pk, notified_at=None, updated_at__lte=cutoff)
        if claim.update(updated_at=now):
            record.updated_at = now
            if _notify_verified(record):
                stats["renotified"] += 1
            else:
                stats["errors"] += 1

    if conf.webhook_event_retention_days is not None:
        expired = now - dt.timedelta(days=conf.webhook_event_retention_days)
        stats["events_pruned"], _ = PeyameshWebhookEvent.objects.filter(received_at__lt=expired).delete()
    return stats


__all__ = [
    "build_callback_url",
    "get_returned_payment",
    "process_callback",
    "process_event",
    "reconcile",
    "refund_payment",
    "return_url_for",
    "settle_payment",
    "start_payment",
]
