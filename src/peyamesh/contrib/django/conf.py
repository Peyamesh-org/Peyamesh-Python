"""``settings.PEYAMESH`` — the single Django setting of this integration.

::

    PEYAMESH = {
        "API_KEY": env("PEYAMESH_API_KEY"),          # required (or the PEYAMESH_API_KEY env var)
        "CURRENCY": "IRT",                           # unit of the amounts you pass: "IRT" (Toman) or "IRR" (Rial)
        "WEBHOOK_SECRET": env("PEYAMESH_WEBHOOK_SECRET"),
        "SUCCESS_URL": "orders:paid",                # URL name, path or absolute URL
        "FAILURE_URL": "orders:payment-failed",
    }
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from django.conf import settings as django_settings
from django.core.signals import setting_changed
from django.dispatch import receiver

SETTING_NAME = "PEYAMESH"

DEFAULTS: dict[str, Any] = {
    "API_KEY": None,
    "BASE_URL": None,
    "CURRENCY": None,
    "TIMEOUT": 20,
    "MAX_RETRIES": 2,
    "WEBHOOK_SECRET": None,
    "WEBHOOK_TOLERANCE": 300,
    "CALLBACK_URL": None,
    "SITE_URL": None,
    "SUCCESS_URL": "/",
    "FAILURE_URL": "/",
    "PENDING_URL": None,
    "RETURN_QUERY_PARAM": "payment",
    "RECONCILE_AFTER_MINUTES": 15,
    "RECONCILE_LIMIT": 200,
    "WEBHOOK_EVENT_RETENTION_DAYS": 30,
}


@dataclass(frozen=True)
class PeyameshSettings:
    api_key: Optional[str]
    base_url: Optional[str]
    currency: Optional[str]
    timeout: float
    max_retries: int
    webhook_secret: tuple[str, ...]
    webhook_tolerance: Optional[int]
    callback_url: Optional[str]
    site_url: Optional[str]
    success_url: str
    failure_url: str
    pending_url: Optional[str]
    return_query_param: str
    reconcile_after_minutes: int
    reconcile_limit: int
    webhook_event_retention_days: Optional[int]


_cached: Optional[PeyameshSettings] = None


def raw_settings() -> dict[str, Any]:
    value = getattr(django_settings, SETTING_NAME, None)
    return dict(value) if isinstance(value, dict) else {}


def _secrets(value: Any) -> tuple[str, ...]:
    if value is None:
        value = os.environ.get("PEYAMESH_WEBHOOK_SECRET")
    if not value:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    return tuple(str(v).strip() for v in value if v and str(v).strip())


def _retention(value: Any) -> Optional[int]:
    # Duplicate deliveries are only recognised while their event is kept: never less than a day.
    return None if value is None else max(1, int(value))


def get_settings() -> PeyameshSettings:
    global _cached
    if _cached is None:
        user = raw_settings()
        merged = {**DEFAULTS, **{k: v for k, v in user.items() if k in DEFAULTS}}
        currency = merged["CURRENCY"]
        _cached = PeyameshSettings(
            api_key=merged["API_KEY"],
            base_url=merged["BASE_URL"],
            currency=str(currency).upper() if currency else None,
            timeout=float(merged["TIMEOUT"]),
            max_retries=int(merged["MAX_RETRIES"]),
            webhook_secret=_secrets(merged["WEBHOOK_SECRET"]),
            webhook_tolerance=None if merged["WEBHOOK_TOLERANCE"] is None else int(merged["WEBHOOK_TOLERANCE"]),
            callback_url=merged["CALLBACK_URL"],
            site_url=merged["SITE_URL"],
            success_url=merged["SUCCESS_URL"] or "/",
            failure_url=merged["FAILURE_URL"] or "/",
            pending_url=merged["PENDING_URL"],
            return_query_param=str(merged["RETURN_QUERY_PARAM"] or "payment"),
            reconcile_after_minutes=int(merged["RECONCILE_AFTER_MINUTES"]),
            reconcile_limit=int(merged["RECONCILE_LIMIT"]),
            webhook_event_retention_days=_retention(merged["WEBHOOK_EVENT_RETENTION_DAYS"]),
        )
    return _cached


def reset_settings() -> None:
    global _cached
    _cached = None


@receiver(setting_changed)
def _on_setting_changed(*, setting: str, **kwargs: Any) -> None:
    if setting == SETTING_NAME:
        reset_settings()
        from .client import reset_client

        reset_client()
