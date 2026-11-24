"""Webhook signature verification.

Each webhook request carries::

    Peyamesh-Signature: t=<unix timestamp>,v1=<hex hmac-sha256(secret, "<t>." + raw_body)>
    Peyamesh-Event-Id:  evt_...          (stable across retries — use it to de-duplicate)
    Peyamesh-Event-Type: payment.succeeded

Always verify against the **raw request body bytes**, before any JSON parsing.

Example (any framework)::

    from peyamesh import webhooks

    event = webhooks.construct_event(
        payload=request_body_bytes,
        signature_header=headers["Peyamesh-Signature"],
        secret="whsec_...",
    )
    if event.type == "payment.succeeded":
        ...
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Iterable
from typing import Optional, Union

from .errors import SignatureVerificationError
from .models import WebhookEvent

SIGNATURE_HEADER = "Peyamesh-Signature"
EVENT_ID_HEADER = "Peyamesh-Event-Id"
EVENT_TYPE_HEADER = "Peyamesh-Event-Type"
DELIVERY_ATTEMPT_HEADER = "Peyamesh-Delivery-Attempt"

#: Maximum accepted age (and clock skew) of a signature, in seconds.
DEFAULT_TOLERANCE = 300
#: Maximum accepted webhook body size (bytes); real events are a few kilobytes.
MAX_PAYLOAD_BYTES = 256 * 1024

Secret = Union[str, Iterable[str]]


def compute_signature(secret: str, timestamp: int, payload: Union[bytes, str]) -> str:
    """Hex HMAC-SHA256 of ``"<timestamp>." + payload`` — the ``v1`` scheme."""
    body = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hmac.new(secret.encode("utf-8"), f"{int(timestamp)}.".encode() + body, hashlib.sha256).hexdigest()


def generate_test_header(payload: Union[bytes, str], secret: str, *, timestamp: Optional[int] = None) -> str:
    """Build a valid ``Peyamesh-Signature`` header — handy for testing your webhook endpoint."""
    ts = int(time.time()) if timestamp is None else int(timestamp)
    return f"t={ts},v1={compute_signature(secret, ts, payload)}"


def _parse_header(header: str) -> tuple[int, list[str]]:
    timestamp: Optional[int] = None
    signatures: list[str] = []
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                raise SignatureVerificationError(
                    "مقدار زمان امضای وبهوک نامعتبر است.", code="invalid_signature_header"
                ) from None
        elif key == "v1" and value:
            signatures.append(value.lower())
    if timestamp is None or not signatures:
        raise SignatureVerificationError("ساختار هدر امضای وبهوک نامعتبر است.", code="invalid_signature_header")
    return timestamp, signatures


def _secrets(secret: Secret) -> list[str]:
    items = [secret] if isinstance(secret, str) else [s for s in secret if s]
    items = [s.strip() for s in items if s and s.strip()]
    if not items:
        raise SignatureVerificationError("کلید امضای وبهوک تنظیم نشده است.", code="webhook_secret_missing")
    return items


def verify_signature(
    payload: Union[bytes, str],
    signature_header: Optional[str],
    secret: Secret,
    *,
    tolerance: Optional[int] = DEFAULT_TOLERANCE,
    now: Optional[float] = None,
) -> int:
    """Verify a webhook signature and return its timestamp.

    ``secret`` may be a list of secrets to support rotation: the signature is accepted if it matches any of
    them. ``tolerance`` (seconds) rejects replays of old deliveries; pass ``None`` to disable that check.

    :raises SignatureVerificationError: on any problem; never returns on failure.
    """
    if not signature_header:
        raise SignatureVerificationError("هدر امضای وبهوک ارسال نشده است.", code="missing_signature")
    body = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if len(body) > MAX_PAYLOAD_BYTES:
        raise SignatureVerificationError("حجم بدنه وبهوک بیش از حد مجاز است.", code="payload_too_large")
    timestamp, candidates = _parse_header(signature_header)
    keys = _secrets(secret)

    matched = False
    for key in keys:
        expected = compute_signature(key, timestamp, body)
        for candidate in candidates:
            # Constant-time comparison; keep looping to avoid leaking which key/candidate matched.
            if hmac.compare_digest(expected, candidate):
                matched = True
    if not matched:
        raise SignatureVerificationError("امضای وبهوک معتبر نیست.")

    if tolerance is not None:
        current = time.time() if now is None else now
        if abs(current - timestamp) > tolerance:
            raise SignatureVerificationError("امضای وبهوک منقضی شده است.", code="signature_expired")
    return timestamp


def construct_event(
    payload: Union[bytes, str],
    signature_header: Optional[str],
    secret: Secret,
    *,
    tolerance: Optional[int] = DEFAULT_TOLERANCE,
) -> WebhookEvent:
    """Verify the signature, then parse the body into a :class:`~peyamesh.models.WebhookEvent`."""
    verify_signature(payload, signature_header, secret, tolerance=tolerance)
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        raise SignatureVerificationError("بدنه وبهوک JSON معتبر نیست.", code="invalid_payload") from None
    if not isinstance(data, dict) or not data.get("id") or not data.get("type"):
        raise SignatureVerificationError("ساختار رویداد وبهوک نامعتبر است.", code="invalid_payload")
    return WebhookEvent.from_dict(data)


__all__ = [
    "DEFAULT_TOLERANCE",
    "DELIVERY_ATTEMPT_HEADER",
    "EVENT_ID_HEADER",
    "EVENT_TYPE_HEADER",
    "SIGNATURE_HEADER",
    "compute_signature",
    "construct_event",
    "generate_test_header",
    "verify_signature",
]
