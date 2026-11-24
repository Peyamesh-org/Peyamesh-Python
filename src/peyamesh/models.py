"""Typed, read-only views of the objects returned by the API.

Objects are parsed leniently: unknown fields are ignored (but kept in ``raw``) so that new server
fields never break existing integrations, and enum-like values are kept as plain strings that compare
equal to the :class:`PaymentStatus` / :class:`Currency` members.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class _StrEnum(str, Enum):
    def __str__(self) -> str:
        return str(self.value)


class Currency(_StrEnum):
    IRR = "IRR"  #: Rial
    IRT = "IRT"  #: Toman (1 Toman = 10 Rials)


class PaymentStatus(_StrEnum):
    CREATED = "created"  #: created, buyer has not opened the payment page yet
    PENDING = "pending"  #: buyer is on the bank page
    PAID = "paid"  #: paid at the bank; **must be verified** by the merchant
    VERIFIED = "verified"  #: paid and verified — the final success state
    FAILED = "failed"
    EXPIRED = "expired"
    UNKNOWN = "unknown"  #: the bank result is still being confirmed
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"


class RefundStatus(_StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELED = "canceled"


class EventType(_StrEnum):
    PAYMENT_CREATED = "payment.created"
    PAYMENT_PENDING = "payment.pending"
    PAYMENT_SUCCEEDED = "payment.succeeded"
    PAYMENT_FAILED = "payment.failed"
    PAYMENT_EXPIRED = "payment.expired"
    PAYMENT_REFUNDED = "payment.refunded"
    REFUND_FAILED = "refund.failed"
    SETTLEMENT_COMPLETED = "settlement.completed"
    SETTLEMENT_FAILED = "settlement.failed"


#: Statuses in which money has been received from the buyer.
PAID_STATUSES = frozenset(
    {PaymentStatus.PAID, PaymentStatus.VERIFIED, PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED}
)
#: Statuses that can still change on their own (buyer still paying, bank result pending).
OPEN_STATUSES = frozenset({PaymentStatus.CREATED, PaymentStatus.PENDING, PaymentStatus.UNKNOWN})
#: Statuses in which the payment did not (and will not) succeed.
FAILED_STATUSES = frozenset({PaymentStatus.FAILED, PaymentStatus.EXPIRED})


# --------------------------------------------------------------------------- parsing helpers
def parse_datetime(value: Any) -> Optional[dt.datetime]:
    """Parse ISO-8601 timestamps (``Z`` suffix included) on every supported Python version."""
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Python < 3.11 only accepts 0, 3 or 6 fractional digits.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        rest = ""
        for i, ch in enumerate(tail):
            if not ch.isdigit():
                rest = tail[i:]
                break
            digits += ch
        text = f"{head}.{digits[:6].ljust(6, '0')}{rest}" if digits else head + rest
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def _int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _opt_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


# --------------------------------------------------------------------------- objects
@dataclass(frozen=True)
class Payment:
    """A payment as seen by the merchant.

    Amounts ending in ``_irr`` are always in Rials; :attr:`amount` is in :attr:`currency`, exactly as
    it was sent when the payment was created.
    """

    authority: str
    status: str
    amount: int
    currency: str
    livemode: bool = True
    amount_irr: int = 0
    fee_irr: int = 0
    fee_bearer: str = ""
    payable_irr: int = 0
    net_irr: int = 0
    refunded_irr: int = 0
    refunded: int = 0
    order_id: str = ""
    description: str = ""
    mobile: str = ""
    email: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    ref_id: Optional[int] = None
    card_pan: str = ""
    card_hash: str = ""
    payment_url: Optional[str] = None
    expires_at: Optional[dt.datetime] = None
    created_at: Optional[dt.datetime] = None
    paid_at: Optional[dt.datetime] = None
    verified_at: Optional[dt.datetime] = None
    failure_code: str = ""
    failure_message: str = ""
    #: ``True`` when the API answered an ``Idempotency-Key`` replay with the original payment.
    replayed: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, replayed: bool = False) -> Payment:
        return cls(
            authority=_str(data.get("authority")),
            status=_str(data.get("status")),
            amount=_int(data.get("amount")),
            currency=_str(data.get("currency")) or Currency.IRR.value,
            livemode=bool(data.get("livemode", True)),
            amount_irr=_int(data.get("amount_irr")),
            fee_irr=_int(data.get("fee_irr")),
            fee_bearer=_str(data.get("fee_bearer")),
            payable_irr=_int(data.get("payable_irr")),
            net_irr=_int(data.get("net_irr")),
            refunded_irr=_int(data.get("refunded_irr")),
            refunded=_int(data.get("refunded")),
            order_id=_str(data.get("order_id")),
            description=_str(data.get("description")),
            mobile=_str(data.get("mobile")),
            email=_str(data.get("email")),
            metadata=_dict(data.get("metadata")),
            ref_id=_opt_int(data.get("ref_id")),
            card_pan=_str(data.get("card_pan")),
            card_hash=_str(data.get("card_hash")),
            payment_url=data.get("payment_url") or None,
            expires_at=parse_datetime(data.get("expires_at")),
            created_at=parse_datetime(data.get("created_at")),
            paid_at=parse_datetime(data.get("paid_at")),
            verified_at=parse_datetime(data.get("verified_at")),
            failure_code=_str(data.get("failure_code")),
            failure_message=_str(data.get("failure_message")),
            replayed=replayed,
            raw=dict(data),
        )

    @property
    def is_paid(self) -> bool:
        """Money was received (verified or not, refunded or not)."""
        return self.status in PAID_STATUSES

    @property
    def is_verified(self) -> bool:
        """Paid *and* verified by the merchant — safe to deliver the order."""
        return self.status in PAID_STATUSES and self.status != PaymentStatus.PAID

    @property
    def needs_verification(self) -> bool:
        return self.status == PaymentStatus.PAID

    @property
    def is_open(self) -> bool:
        """The payment can still change on its own (buyer paying, or bank result being confirmed)."""
        return self.status in OPEN_STATUSES

    @property
    def is_failed(self) -> bool:
        return self.status in FAILED_STATUSES


@dataclass(frozen=True)
class VerifyResult:
    """Result of :meth:`Peyamesh.verify_payment`. ``code`` is ``100`` (verified now) or ``101`` (already)."""

    code: int
    message: str
    payment: Payment

    @property
    def already_verified(self) -> bool:
        return self.code == 101

    @property
    def ref_id(self) -> Optional[int]:
        return self.payment.ref_id

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VerifyResult:
        return cls(code=_int(data.get("code"), 100), message=_str(data.get("message")), payment=Payment.from_dict(data))


@dataclass(frozen=True)
class Refund:
    id: str
    authority: str
    amount_irr: int
    status: str
    returned_fee_irr: int = 0
    reason: str = ""
    method: str = ""
    channel: str = ""
    destination_iban: str = ""
    destination_card: str = ""
    bank_reference: str = ""
    failure_reason: str = ""
    created_at: Optional[dt.datetime] = None
    reviewed_at: Optional[dt.datetime] = None
    processed_at: Optional[dt.datetime] = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Refund:
        return cls(
            id=_str(data.get("id")),
            authority=_str(data.get("authority")),
            amount_irr=_int(data.get("amount_irr")),
            status=_str(data.get("status")),
            returned_fee_irr=_int(data.get("returned_fee_irr")),
            reason=_str(data.get("reason")),
            method=_str(data.get("method")),
            channel=_str(data.get("channel")),
            destination_iban=_str(data.get("destination_iban")),
            destination_card=_str(data.get("destination_card")),
            bank_reference=_str(data.get("bank_reference")),
            failure_reason=_str(data.get("failure_reason")),
            created_at=parse_datetime(data.get("created_at")),
            reviewed_at=parse_datetime(data.get("reviewed_at")),
            processed_at=parse_datetime(data.get("processed_at")),
            raw=dict(data),
        )

    @property
    def is_final(self) -> bool:
        return self.status in {
            RefundStatus.SUCCEEDED,
            RefundStatus.FAILED,
            RefundStatus.REJECTED,
            RefundStatus.CANCELED,
        }


@dataclass(frozen=True)
class WebhookEvent:
    """An authenticated webhook event (see :func:`peyamesh.webhooks.construct_event`)."""

    id: str
    type: str
    livemode: bool
    data: Mapping[str, Any]
    created_at: Optional[dt.datetime] = None
    #: ``True`` for the test events sent from the dashboard ("send test event").
    test: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WebhookEvent:
        return cls(
            id=_str(data.get("id")),
            type=_str(data.get("type")),
            livemode=bool(data.get("livemode", True)),
            data=_dict(data.get("data")),
            created_at=parse_datetime(data.get("created_at")),
            test=bool(data.get("test", False)),
            raw=dict(data),
        )

    @property
    def object_type(self) -> str:
        return _str(self.data.get("object"))

    @property
    def payment(self) -> Optional[Payment]:
        """The payment carried by ``payment.*`` events."""
        if self.object_type == "payment":
            return Payment.from_dict(self.data)
        return None

    @property
    def refund(self) -> Optional[Refund]:
        """The refund carried by ``refund.*`` events, or attached to ``payment.refunded``."""
        if self.object_type == "refund":
            return Refund.from_dict(self.data)
        nested = self.data.get("refund")
        if isinstance(nested, Mapping):
            return Refund.from_dict(nested)
        return None


__all__ = [
    "FAILED_STATUSES",
    "OPEN_STATUSES",
    "PAID_STATUSES",
    "Currency",
    "EventType",
    "Payment",
    "PaymentStatus",
    "Refund",
    "RefundStatus",
    "VerifyResult",
    "WebhookEvent",
    "parse_datetime",
]
