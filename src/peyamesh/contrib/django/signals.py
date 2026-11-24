"""Signals sent by the integration — connect your order fulfilment here.

All signals are sent with ``sender=PeyameshPayment`` after the database transaction commits, and each
payment-state signal is sent **once** per state change, even when the buyer's return, a webhook and the
reconciler handle the same payment concurrently. Exceptions raised by receivers are logged (logger
``peyamesh.django``) and never break the buyer's redirect or the webhook response.

``payment_verified`` is the one that must never be lost: if one of its receivers raises (or the process dies
before they ran), ``reconcile`` sends it again — to every receiver — until all of them succeed, for up to
3 days. Write its receivers so that running them twice is harmless.

::

    from django.dispatch import receiver
    from peyamesh.contrib.django.signals import payment_verified

    @receiver(payment_verified)
    def deliver_order(sender, payment, **kwargs):
        # Idempotent: a second call finds the order already paid and does nothing.
        if Order.objects.filter(pk=payment.order_id, paid=False).update(paid=True):
            send_receipt(payment.order_id)
"""

from django.dispatch import Signal

#: A payment was verified (``payment``). Deliver the order here.
payment_verified = Signal()
#: A payment failed, was canceled, or expired (``payment``).
payment_failed = Signal()
#: Money was refunded (``payment``, ``refund`` — a :class:`peyamesh.Refund` or ``None``).
payment_refunded = Signal()
#: Any authenticated webhook event, after it was processed (``event``, ``payment`` — may be ``None``).
webhook_received = Signal()

__all__ = ["payment_failed", "payment_refunded", "payment_verified", "webhook_received"]
