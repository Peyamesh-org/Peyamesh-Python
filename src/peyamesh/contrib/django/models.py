from __future__ import annotations

import uuid
from typing import Any

from django.db import models
from django.http import HttpResponseRedirect

from .._flow import LocalStatus


class PeyameshPaymentQuerySet(models.QuerySet):
    def paid(self) -> PeyameshPaymentQuerySet:
        """Verified payments (refunded ones included) — the orders you can deliver."""
        return self.filter(status__in=LocalStatus.PAID)

    def pending(self) -> PeyameshPaymentQuerySet:
        return self.filter(status__in=(LocalStatus.NEW, LocalStatus.PENDING))

    def failed(self) -> PeyameshPaymentQuerySet:
        return self.filter(status__in=(LocalStatus.FAILED, LocalStatus.ERROR))

    def for_order(self, order_id: Any) -> PeyameshPaymentQuerySet:
        return self.filter(order_id=str(order_id))

    def unnotified(self) -> PeyameshPaymentQuerySet:
        """Paid payments whose ``payment_verified`` receivers have not all succeeded yet."""
        return self.paid().filter(notified_at=None)


class PeyameshPayment(models.Model):
    """Local record of a payment, kept in sync with the gateway by this integration."""

    Status = LocalStatus

    public_id = models.UUIDField("شناسه", default=uuid.uuid4, unique=True, editable=False)
    authority = models.CharField("Authority", max_length=64, unique=True, null=True, blank=True)
    status = models.CharField(
        "وضعیت", max_length=24, choices=LocalStatus.CHOICES, default=LocalStatus.NEW, db_index=True
    )
    remote_status = models.CharField("وضعیت در درگاه", max_length=24, blank=True, default="")
    livemode = models.BooleanField("عملیاتی", null=True, blank=True)

    amount = models.BigIntegerField("مبلغ")
    currency = models.CharField("واحد پول", max_length=3, blank=True, default="")
    amount_irr = models.BigIntegerField("مبلغ (ریال)", null=True, blank=True)
    fee_irr = models.BigIntegerField("کارمزد (ریال)", default=0)
    refunded_irr = models.BigIntegerField("بازپرداخت‌شده (ریال)", default=0)

    order_id = models.CharField("شماره سفارش", max_length=100, blank=True, default="", db_index=True)
    description = models.CharField("توضیحات", max_length=500, blank=True, default="")
    mobile = models.CharField("موبایل", max_length=20, blank=True, default="")
    email = models.EmailField("ایمیل", blank=True, default="")
    metadata = models.JSONField("متادیتا", default=dict, blank=True)

    payment_url = models.URLField("آدرس پرداخت", max_length=2048, blank=True, default="")
    callback_url = models.URLField("آدرس بازگشت", max_length=2048, blank=True, default="")
    return_url = models.CharField("آدرس نتیجه", max_length=2048, blank=True, default="")

    ref_id = models.CharField("شماره پیگیری", max_length=32, blank=True, default="", db_index=True)
    card_pan = models.CharField("کارت", max_length=32, blank=True, default="")
    card_hash = models.CharField("هش کارت", max_length=128, blank=True, default="")
    failure_code = models.CharField("کد خطا", max_length=64, blank=True, default="")
    failure_message = models.CharField("پیام خطا", max_length=500, blank=True, default="")

    expires_at = models.DateTimeField("مهلت پرداخت", null=True, blank=True)
    verified_at = models.DateTimeField("زمان تأیید", null=True, blank=True)
    #: When every ``payment_verified`` receiver ran without raising. Empty on a paid payment means a receiver
    #: failed (or never ran): ``reconcile`` sends the signal again.
    notified_at = models.DateTimeField("اعلام تأیید به فروشگاه", null=True, blank=True)
    last_synced_at = models.DateTimeField("آخرین همگام‌سازی", null=True, blank=True)
    created_at = models.DateTimeField("ایجاد", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("به‌روزرسانی", auto_now=True)

    objects = PeyameshPaymentQuerySet.as_manager()

    class Meta:
        verbose_name = "پرداخت پیامش"
        verbose_name_plural = "پرداخت‌های پیامش"
        ordering = ("-created_at",)
        indexes = (models.Index(fields=("status", "created_at"), name="peyamesh_status_created"),)

    def __str__(self) -> str:
        label = f"سفارش {self.order_id}" if self.order_id else str(self.public_id)[:8]
        return f"{label} — {self.get_status_display()}"

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

    def redirect(self) -> HttpResponseRedirect:
        """Send the buyer to the payment page."""
        if not self.payment_url:
            raise ValueError("This payment has no payment URL (it was not registered at the gateway).")
        return HttpResponseRedirect(self.payment_url)


class PeyameshWebhookEvent(models.Model):
    """Processed webhook events, used to ignore duplicate deliveries."""

    event_id = models.CharField("شناسه رویداد", max_length=64, unique=True)
    type = models.CharField("نوع", max_length=64, db_index=True)
    livemode = models.BooleanField("عملیاتی", default=True)
    payload = models.JSONField("محتوا", default=dict)
    received_at = models.DateTimeField("دریافت", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "رویداد وبهوک پیامش"
        verbose_name_plural = "رویدادهای وبهوک پیامش"
        ordering = ("-received_at",)

    def __str__(self) -> str:
        return f"{self.type} ({self.event_id})"
