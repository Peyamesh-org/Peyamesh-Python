"""Plug-and-play Django integration.

1. ``INSTALLED_APPS += ["peyamesh.contrib.django"]`` and ``python manage.py migrate``
2. ``path("peyamesh/", include("peyamesh.contrib.django.urls"))`` in your root ``urls.py``
3. ``settings.PEYAMESH = {"API_KEY": ..., "CURRENCY": "IRT", "WEBHOOK_SECRET": ..., ...}``
4. In your checkout view::

       from peyamesh.contrib.django import start_payment

       payment = start_payment(request, amount=order.total, order_id=order.pk)
       return payment.redirect()

5. Deliver orders in a :data:`~peyamesh.contrib.django.signals.payment_verified` receiver.
6. Schedule ``python manage.py peyamesh_reconcile`` every few minutes.
"""

from __future__ import annotations

from typing import Any

_SERVICES = {
    "build_callback_url",
    "get_returned_payment",
    "reconcile",
    "refund_payment",
    "settle_payment",
    "start_payment",
}


def __getattr__(name: str) -> Any:  # lazy: importing models before apps are ready is not allowed
    if name in _SERVICES:
        from . import services

        return getattr(services, name)
    if name in ("PeyameshPayment", "PeyameshWebhookEvent"):
        from . import models

        return getattr(models, name)
    if name == "override_client":
        from .client import override_client

        return override_client
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = sorted([*_SERVICES, "PeyameshPayment", "PeyameshWebhookEvent", "override_client"])
