"""``manage.py check`` diagnostics for ``settings.PEYAMESH``."""

from __future__ import annotations

import os
from typing import Any

from django.conf import settings
from django.core.checks import CheckMessage, Error, Tags, Warning, register
from django.urls import NoReverseMatch, reverse

from .conf import DEFAULTS, SETTING_NAME


@register()
def check_peyamesh_settings(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    messages: list[CheckMessage] = []
    raw = getattr(settings, SETTING_NAME, None)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return [Error("settings.PEYAMESH must be a dict.", id="peyamesh.E001")]

    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        messages.append(
            Warning(
                f"Unknown key(s) in settings.PEYAMESH: {', '.join(unknown)}.",
                hint=f"Valid keys: {', '.join(sorted(DEFAULTS))}.",
                id="peyamesh.W001",
            )
        )
    if not (raw.get("API_KEY") or os.environ.get("PEYAMESH_API_KEY")):
        messages.append(
            Error(
                "The Peyamesh API key is not configured.",
                hint="Set PEYAMESH['API_KEY'] or the PEYAMESH_API_KEY environment variable.",
                id="peyamesh.E002",
            )
        )
    currency = raw.get("CURRENCY")
    if currency and str(currency).upper() not in ("IRR", "IRT"):
        messages.append(Error("PEYAMESH['CURRENCY'] must be 'IRT' (Toman) or 'IRR' (Rial).", id="peyamesh.E003"))
    if not (raw.get("WEBHOOK_SECRET") or os.environ.get("PEYAMESH_WEBHOOK_SECRET")):
        messages.append(
            Warning(
                "PEYAMESH['WEBHOOK_SECRET'] is not set: webhooks will be rejected.",
                hint="Webhooks let the shop learn about payments even when the buyer never returns. Copy the "
                "endpoint secret from the dashboard.",
                id="peyamesh.W002",
            )
        )
    retention = raw.get("WEBHOOK_EVENT_RETENTION_DAYS", 30)
    if retention is not None and (not isinstance(retention, int) or isinstance(retention, bool) or retention < 1):
        messages.append(
            Error(
                "PEYAMESH['WEBHOOK_EVENT_RETENTION_DAYS'] must be a whole number of days (at least 1) or None.",
                id="peyamesh.E005",
            )
        )
    if not raw.get("CURRENCY"):
        messages.append(
            Warning(
                "PEYAMESH['CURRENCY'] is not set: amounts will be read in the shop's default currency.",
                hint="Set it explicitly to 'IRT' or 'IRR' to avoid Rial/Toman confusion.",
                id="peyamesh.W003",
            )
        )
    return messages


@register(Tags.urls)
def check_peyamesh_urls(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    raw = getattr(settings, SETTING_NAME, None)
    if isinstance(raw, dict) and raw.get("CALLBACK_URL"):
        return []
    try:
        reverse("peyamesh:callback")
    except NoReverseMatch:
        return [
            Error(
                "The Peyamesh callback URL cannot be resolved.",
                hint='Add path("peyamesh/", include("peyamesh.contrib.django.urls")) to your root urls.py, '
                "or set PEYAMESH['CALLBACK_URL'].",
                id="peyamesh.E004",
            )
        ]
    return []
