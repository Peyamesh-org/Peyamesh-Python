from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any, Optional, Union


def _env_bool_int(value: str) -> Optional[int]:
    return None if value.strip().lower() in ("", "none", "off") else int(value)


@dataclass
class PeyameshSettings:
    """Configuration of :class:`~peyamesh.contrib.fastapi.PeyameshFastAPI`.

    Every field can come from the environment with :meth:`from_env` (``PEYAMESH_API_KEY``,
    ``PEYAMESH_CURRENCY``, ``PEYAMESH_WEBHOOK_SECRET``, ``PEYAMESH_SUCCESS_URL`` ...).
    """

    api_key: Optional[str] = None
    base_url: Optional[str] = None
    #: Unit of the amounts you pass: ``"IRT"`` (Toman) or ``"IRR"`` (Rial).
    currency: Optional[str] = None
    timeout: float = 20.0
    max_retries: int = 2
    #: Webhook endpoint secret(s). Several values (list, or comma-separated in the env) allow rotation.
    webhook_secret: Union[str, list[str], None] = None
    webhook_tolerance: Optional[int] = 300
    #: Absolute callback URL. Default: built from the incoming request (``request.url_for``).
    callback_url: Optional[str] = None
    #: Where the buyer lands after paying; ``?payment=<id>&status=success|failed|pending`` is appended.
    success_url: str = "/"
    failure_url: str = "/"
    pending_url: Optional[str] = None
    return_query_param: str = "payment"
    reconcile_after_minutes: int = 15
    reconcile_limit: int = 200
    #: Processed webhook events are kept this many days to recognise duplicate deliveries; ``None`` = forever.
    webhook_event_retention_days: Optional[int] = 30

    def __post_init__(self) -> None:
        if self.webhook_event_retention_days is not None and self.webhook_event_retention_days < 1:
            raise ValueError("webhook_event_retention_days must be at least 1 (or None to keep events forever)")
        if self.currency:
            self.currency = str(self.currency).upper()
            if self.currency not in ("IRR", "IRT"):
                raise ValueError("currency must be 'IRT' (Toman) or 'IRR' (Rial)")

    @property
    def webhook_secrets(self) -> list[str]:
        value = self.webhook_secret
        if not value:
            return []
        items = value.split(",") if isinstance(value, str) else list(value)
        return [s.strip() for s in items if s and s.strip()]

    @classmethod
    def from_env(
        cls, prefix: str = "PEYAMESH_", environ: Optional[Mapping[str, str]] = None, **overrides: Any
    ) -> PeyameshSettings:
        env = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for f in fields(cls):
            raw = env.get(prefix + f.name.upper())
            if raw is None:
                continue
            if f.name in ("timeout",):
                values[f.name] = float(raw)
            elif f.name in ("max_retries", "reconcile_after_minutes", "reconcile_limit"):
                values[f.name] = int(raw)
            elif f.name in ("webhook_tolerance", "webhook_event_retention_days"):
                values[f.name] = _env_bool_int(raw)
            else:
                values[f.name] = raw
        values.update(overrides)
        return cls(**values)


__all__ = ["PeyameshSettings"]
