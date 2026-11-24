from __future__ import annotations

from typing import Any

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from ...errors import PeyameshError
from .models import PeyameshPayment, PeyameshWebhookEvent


class _ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(PeyameshPayment)
class PeyameshPaymentAdmin(_ReadOnlyAdmin):
    list_display = ("__str__", "amount_display", "status", "livemode", "ref_id", "card_pan", "created_at")
    list_filter = ("status", "livemode", ("notified_at", admin.EmptyFieldListFilter), "currency", "created_at")
    search_fields = ("authority", "order_id", "ref_id", "mobile", "email", "card_pan")
    date_hierarchy = "created_at"
    list_per_page = 50
    actions = ("sync_with_gateway",)
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "public_id",
                    "status",
                    "order_id",
                    "amount",
                    "currency",
                    "amount_irr",
                    "fee_irr",
                    "refunded_irr",
                    "livemode",
                )
            },
        ),
        (
            "درگاه",
            {
                "fields": (
                    "authority",
                    "remote_status",
                    "ref_id",
                    "card_pan",
                    "card_hash",
                    "payment_url",
                    "failure_code",
                    "failure_message",
                )
            },
        ),
        ("خریدار", {"fields": ("description", "mobile", "email", "metadata")}),
        ("آدرس‌ها", {"fields": ("callback_url", "return_url"), "classes": ("collapse",)}),
        (
            "زمان‌ها",
            {"fields": ("created_at", "expires_at", "verified_at", "notified_at", "last_synced_at", "updated_at")},
        ),
    )

    @admin.display(description="مبلغ", ordering="amount")
    def amount_display(self, obj: PeyameshPayment) -> str:
        unit = {"IRT": "تومان", "IRR": "ریال"}.get(obj.currency, obj.currency)
        return f"{obj.amount:,} {unit}".strip()

    @admin.action(description="همگام‌سازی با درگاه")
    def sync_with_gateway(self, request: HttpRequest, queryset: QuerySet[PeyameshPayment]) -> None:
        from .services import settle_payment

        done = failed = 0
        for record in queryset[:200]:
            try:
                settle_payment(record)
                done += 1
            except PeyameshError:
                failed += 1
        self.message_user(request, f"{done} پرداخت همگام‌سازی شد.", messages.SUCCESS)
        if failed:
            self.message_user(request, f"{failed} پرداخت به دلیل خطای ارتباط همگام نشد.", messages.WARNING)


@admin.register(PeyameshWebhookEvent)
class PeyameshWebhookEventAdmin(_ReadOnlyAdmin):
    list_display = ("event_id", "type", "livemode", "received_at")
    list_filter = ("type", "livemode")
    search_fields = ("event_id",)
    date_hierarchy = "received_at"
