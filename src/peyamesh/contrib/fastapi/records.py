from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from starlette.responses import RedirectResponse

from .._flow import LocalStatus


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclass
class PaymentRecord:
    """Local record of a payment (one row of the ``peyamesh_payments`` table)."""

    amount: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    authority: Optional[str] = None
    status: str = LocalStatus.NEW
    remote_status: str = ""
    livemode: Optional[bool] = None
    currency: str = ""
    amount_irr: Optional[int] = None
    fee_irr: int = 0
    refunded_irr: int = 0
    order_id: str = ""
    description: str = ""
    mobile: str = ""
    email: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    payment_url: str = ""
    callback_url: str = ""
    return_url: str = ""
    ref_id: str = ""
    card_pan: str = ""
    card_hash: str = ""
    failure_code: str = ""
    failure_message: str = ""
    expires_at: Optional[dt.datetime] = None
    verified_at: Optional[dt.datetime] = None
    #: When every ``on_payment_verified`` hook ran without raising. Empty on a paid payment means a hook
    #: failed (or never ran): :meth:`PeyameshFastAPI.reconcile` runs the hooks again.
    notified_at: Optional[dt.datetime] = None
    last_synced_at: Optional[dt.datetime] = None
    created_at: dt.datetime = field(default_factory=utcnow)
    updated_at: dt.datetime = field(default_factory=utcnow)

    @property
    def is_paid(self) -> bool:
        """Paid and verified — safe to deliver the order."""
        return self.status in LocalStatus.PAID

    @property
    def is_pending(self) -> bool:
        return self.status in (LocalStatus.NEW, LocalStatus.PENDING)

    @property
    def is_failed(self) -> bool:
        return self.status in (LocalStatus.FAILED, LocalStatus.ERROR)

    @property
    def refundable_irr(self) -> int:
        return max(0, (self.amount_irr or 0) - self.refunded_irr) if self.is_paid else 0

    def redirect(self, status_code: int = 303) -> RedirectResponse:
        """Send the buyer to the payment page (``303 See Other`` works after POST and GET)."""
        if not self.payment_url:
            raise ValueError("This payment has no payment URL (it was not registered at the gateway).")
        return RedirectResponse(self.payment_url, status_code=status_code)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["PaymentRecord", "utcnow"]
