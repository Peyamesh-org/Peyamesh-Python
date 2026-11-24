"""Plug-and-play FastAPI integration.

::

    from fastapi import FastAPI, Request
    from peyamesh.contrib.fastapi import PeyameshFastAPI, PeyameshSettings, SQLAlchemyStore

    peyamesh = PeyameshFastAPI(PeyameshSettings.from_env(), store=SQLAlchemyStore("sqlite+aiosqlite:///shop.db"))
    app = FastAPI(lifespan=peyamesh.lifespan)
    app.include_router(peyamesh.router, prefix="/peyamesh")
"""

from .._flow import LocalStatus, Outcome
from .config import PeyameshSettings
from .integration import PeyameshFastAPI
from .records import PaymentRecord
from .stores import MemoryStore, PaymentStore, SQLAlchemyStore

__all__ = [
    "LocalStatus",
    "MemoryStore",
    "Outcome",
    "PaymentRecord",
    "PaymentStore",
    "PeyameshFastAPI",
    "PeyameshSettings",
    "SQLAlchemyStore",
]
