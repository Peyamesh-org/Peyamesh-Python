"""Official Python SDK for the Peyamesh payment gateway.

Quick start::

    from peyamesh import Peyamesh

    client = Peyamesh(api_key="...", currency="IRT")
    payment = client.create_payment(amount=245_000, callback_url="https://shop.ir/pay/back", order_id="1048")
    # redirect the buyer to payment.payment_url, then on return:
    result = client.verify_payment(payment.authority, amount=245_000)

Framework integrations: :mod:`peyamesh.contrib.django` and :mod:`peyamesh.contrib.fastapi`.
"""

from . import webhooks
from ._base import DEFAULT_BASE_URL
from ._version import __version__
from .async_client import AsyncPeyamesh
from .callback import CallbackParams, InvalidCallbackError, parse_callback
from .client import Peyamesh
from .errors import (
    AmountMismatchError,
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PaymentNotPaidError,
    PermissionDeniedError,
    PeyameshError,
    RateLimitError,
    ServerError,
    ServiceUnavailableError,
    SignatureVerificationError,
)
from .models import (
    Currency,
    EventType,
    Payment,
    PaymentStatus,
    Refund,
    RefundStatus,
    VerifyResult,
    WebhookEvent,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "APIConnectionError",
    "APIError",
    "APITimeoutError",
    "AmountMismatchError",
    "AsyncPeyamesh",
    "AuthenticationError",
    "CallbackParams",
    "ConfigurationError",
    "ConflictError",
    "Currency",
    "EventType",
    "InvalidCallbackError",
    "InvalidRequestError",
    "NotFoundError",
    "Payment",
    "PaymentNotPaidError",
    "PaymentStatus",
    "PermissionDeniedError",
    "Peyamesh",
    "PeyameshError",
    "RateLimitError",
    "Refund",
    "RefundStatus",
    "ServerError",
    "ServiceUnavailableError",
    "SignatureVerificationError",
    "VerifyResult",
    "WebhookEvent",
    "__version__",
    "parse_callback",
    "webhooks",
]
