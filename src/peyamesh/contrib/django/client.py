"""The shared, thread-safe API client built from ``settings.PEYAMESH``."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Optional

from ...client import Peyamesh
from .conf import get_settings

_client: Optional[Peyamesh] = None
_override: Optional[Peyamesh] = None
_lock = threading.Lock()


def get_client() -> Peyamesh:
    """Return the process-wide :class:`~peyamesh.Peyamesh` client (connection pooling included)."""
    global _client
    if _override is not None:
        return _override
    if _client is None:
        with _lock:
            if _client is None:
                conf = get_settings()
                _client = Peyamesh(
                    conf.api_key,
                    base_url=conf.base_url,
                    timeout=conf.timeout,
                    max_retries=conf.max_retries,
                    currency=conf.currency,
                )
    return _client


def reset_client() -> None:
    global _client
    with _lock:
        if _client is not None:
            _client.close()
        _client = None


@contextmanager
def override_client(client: Peyamesh) -> Iterator[Peyamesh]:
    """Use ``client`` instead of the configured one — typically a :class:`peyamesh.testing.FakeGateway`
    client in your test-suite::

        with override_client(gateway.client()):
            response = client.post("/checkout/")
    """
    global _override
    previous = _override
    _override = client
    try:
        yield client
    finally:
        _override = previous
