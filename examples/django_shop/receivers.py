from django.dispatch import receiver

from peyamesh.contrib.django.signals import payment_failed, payment_refunded, payment_verified

from .models import Order


@receiver(payment_verified)
def mark_paid(sender, payment, **kwargs):
    # Sent once per payment, after commit — and again by `peyamesh_reconcile` if a receiver raised.
    # The status filter makes a second call harmless.
    updated = Order.objects.filter(pk=payment.order_id, status="unpaid").update(
        status="paid", payment_ref=payment.ref_id
    )
    paid_by_another = Order.objects.filter(pk=payment.order_id, status="paid").exclude(payment_ref=payment.ref_id)
    if not updated and paid_by_another.exists() and payment.refundable_irr:
        # The same order was paid twice (two browser tabs): give this second payment back.
        from peyamesh.contrib.django import refund_payment

        refund_payment(payment, reason="پرداخت تکراری سفارش", idempotency_key=f"duplicate-{payment.public_id}")


@receiver(payment_failed)
def release(sender, payment, **kwargs):
    Order.objects.filter(pk=payment.order_id, status="unpaid").update(status="payment_failed")


@receiver(payment_refunded)
def refunded(sender, payment, refund, **kwargs):
    if payment.status == "refunded":
        Order.objects.filter(pk=payment.order_id).update(status="refunded")
