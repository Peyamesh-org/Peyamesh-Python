"""Framework-independent rules shared by the Django and FastAPI integrations.

Both integrations keep a local record of every payment and drive it through the same small state machine::

    new ──(registered at the gateway)──▶ pending ──(verified)──▶ verified ──▶ partially_refunded / refunded
     │                                     │  ▲
     └──▶ error (never registered)          └──┴──▶ failed (failed / canceled / expired; may still turn verified
                                                     if the bank confirms a late payment)

Every transition is a *conditional* update (``UPDATE ... WHERE status IN (...)``), so when the buyer's
return, a webhook and the reconciler race on the same payment, exactly one of them wins and the
"payment verified" hook fires once — on every database, without explicit locks.

Delivering the order must not depend on that single call succeeding: ``notified_at`` is set only once
every "payment verified" hook has run without raising. Until then (a hook raised, or the process died
right after the payment was verified) the reconciler calls the hooks again, for up to
:data:`NOTIFY_RETRY_WINDOW` after verification. Hooks must therefore be idempotent.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..models import Payment, PaymentStatus


class LocalStatus:
    NEW = "new"
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"
    ERROR = "error"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"

    CHOICES = (
        (NEW, "ثبت اولیه"),
        (PENDING, "در انتظار پرداخت"),
        (VERIFIED, "پرداخت موفق"),
        (FAILED, "ناموفق"),
        (ERROR, "خطا در ایجاد پرداخت"),
        (PARTIALLY_REFUNDED, "بازپرداخت جزئی"),
        (REFUNDED, "بازپرداخت کامل"),
    )

    #: Money received and verified — deliver the order.
    PAID = frozenset({VERIFIED, PARTIALLY_REFUNDED, REFUNDED})
    #: States from which a successful verification may still move the record to ``verified``.
    VERIFIABLE = frozenset({NEW, PENDING, FAILED})
    #: States that a failure notification may move to ``failed``.
    FAILABLE = frozenset({NEW, PENDING})


#: How long after verification the reconciler keeps re-running "payment verified" hooks that failed.
NOTIFY_RETRY_WINDOW = dt.timedelta(days=3)


class Outcome:
    """What the buyer is told when coming back from the payment page."""

    SUCCESS = "success"
    FAILED = "failed"
    PENDING = "pending"


def local_status_for(remote_status: Optional[str]) -> Optional[str]:
    """Map a server payment status to a local status (``None`` = paid but still needs ``verify``)."""
    status = str(remote_status or "")
    if status == PaymentStatus.VERIFIED:
        return LocalStatus.VERIFIED
    if status == PaymentStatus.PARTIALLY_REFUNDED:
        return LocalStatus.PARTIALLY_REFUNDED
    if status == PaymentStatus.REFUNDED:
        return LocalStatus.REFUNDED
    if status in (PaymentStatus.FAILED, PaymentStatus.EXPIRED):
        return LocalStatus.FAILED
    if status == PaymentStatus.PAID:
        return None
    return LocalStatus.PENDING


def outcome_for(local_status: str) -> str:
    if local_status in LocalStatus.PAID:
        return Outcome.SUCCESS
    if local_status in (LocalStatus.FAILED, LocalStatus.ERROR):
        return Outcome.FAILED
    return Outcome.PENDING


def remote_fields(payment: Payment) -> dict[str, Any]:
    """Fields copied from a server payment onto the local record after each sync."""
    fields: dict[str, Any] = {
        "remote_status": payment.status,
        "livemode": payment.livemode,
        "refunded_irr": payment.refunded_irr,
        "failure_code": payment.failure_code[:64],
        "failure_message": payment.failure_message[:500],
    }
    if payment.amount_irr:
        fields["amount_irr"] = payment.amount_irr
    if payment.fee_irr:
        fields["fee_irr"] = payment.fee_irr
    if payment.ref_id:
        fields["ref_id"] = str(payment.ref_id)
    if payment.card_pan:
        fields["card_pan"] = payment.card_pan[:32]
    if payment.card_hash:
        fields["card_hash"] = payment.card_hash[:128]
    if payment.expires_at:
        fields["expires_at"] = payment.expires_at
    if payment.verified_at:
        fields["verified_at"] = payment.verified_at
    return fields


def refund_status_for(amount_irr: Optional[int], refunded_irr: int) -> Optional[str]:
    if refunded_irr <= 0:
        return None
    if amount_irr and refunded_irr >= amount_irr:
        return LocalStatus.REFUNDED
    return LocalStatus.PARTIALLY_REFUNDED


def with_query(url: str, params: Mapping[str, Any]) -> str:
    """Append query parameters to ``url`` (relative or absolute), replacing existing ones with the same name."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in params]
    query.extend((k, str(v)) for k, v in params.items() if v is not None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


__all__ = [
    "NOTIFY_RETRY_WINDOW",
    "LocalStatus",
    "Outcome",
    "local_status_for",
    "outcome_for",
    "refund_status_for",
    "remote_fields",
    "with_query",
]
