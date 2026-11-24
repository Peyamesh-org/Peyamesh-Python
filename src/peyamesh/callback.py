"""Parsing of the buyer's return to ``callback_url``.

After the bank page, the buyer is sent back to the ``callback_url`` given when the payment was created,
with these query parameters::

    ?Authority=<authority>&Status=OK|NOK[&pm_status=unknown]

.. warning::
   These parameters come from the buyer's browser and can be forged. ``Status=OK`` is **not** a proof of
   payment: always call :meth:`peyamesh.Peyamesh.verify_payment` with the amount stored on your side
   before delivering anything.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

from .errors import PeyameshError

_AUTHORITY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class InvalidCallbackError(PeyameshError):
    """The return URL does not carry a valid ``Authority``."""

    def __init__(self, message: str = "پارامترهای بازگشت از درگاه معتبر نیست.") -> None:
        super().__init__(message, code="invalid_callback")


@dataclass(frozen=True)
class CallbackParams:
    authority: str
    status: str
    pm_status: str = ""

    @property
    def is_ok(self) -> bool:
        """The buyer *claims* a successful payment. Verify before trusting it."""
        return self.status.upper() == "OK"

    @property
    def is_pending(self) -> bool:
        """The bank result is still being confirmed; it will be settled automatically."""
        return self.pm_status.lower() == "unknown"


def _first(params: Any, *names: str) -> Optional[str]:
    getter = getattr(params, "get", None)
    if getter is None:
        return None
    for name in names:
        value = getter(name)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value not in (None, ""):
            return str(value).strip()
    return None


def parse_callback(params: Mapping[str, Any] | Any) -> CallbackParams:
    """Read ``Authority`` / ``Status`` from any mapping of query parameters.

    Works with ``dict``, Django ``request.GET``, Starlette/FastAPI ``request.query_params`` and
    ``urllib.parse.parse_qs`` output. Parameter names are matched case-insensitively.
    """
    authority = _first(params, "Authority", "authority", "AUTHORITY")
    if not authority or not _AUTHORITY_RE.match(authority):
        raise InvalidCallbackError()
    status = (_first(params, "Status", "status", "STATUS") or "NOK").upper()
    pm_status = _first(params, "pm_status", "PM_STATUS") or ""
    return CallbackParams(authority=authority, status=status if status in ("OK", "NOK") else "NOK", pm_status=pm_status)


__all__ = ["CallbackParams", "InvalidCallbackError", "parse_callback"]
