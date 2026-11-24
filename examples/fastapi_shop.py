"""Minimal FastAPI shop using the Peyamesh integration.

    pip install "peyamesh[fastapi]" aiosqlite uvicorn
    export PEYAMESH_API_KEY=...            # a *test* key from the dashboard
    export PEYAMESH_WEBHOOK_SECRET=whsec_...
    uvicorn fastapi_shop:app --reload

Then open http://127.0.0.1:8000/ and press "pay".
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from peyamesh import PeyameshError
from peyamesh.contrib.fastapi import PeyameshFastAPI, PeyameshSettings, SQLAlchemyStore

peyamesh = PeyameshFastAPI(
    PeyameshSettings.from_env(currency="IRT", success_url="/result", failure_url="/result"),
    store=SQLAlchemyStore("sqlite+aiosqlite:///shop.db"),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with peyamesh.lifespan(app):
        reconciler = asyncio.create_task(peyamesh.run_reconciler(interval=300))
        yield
        reconciler.cancel()


app = FastAPI(lifespan=lifespan)
app.include_router(peyamesh.router, prefix="/peyamesh")


@peyamesh.on_payment_verified
async def deliver(payment) -> None:
    # Mark the order as paid here, idempotently: if a hook raises, `reconcile` runs the hooks again.
    print(f"order {payment.order_id} paid, ref_id={payment.ref_id}")


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    return '<form method="post" action="/checkout/1001"><button>pay 50,000 Toman</button></form>'


@app.post("/checkout/{order_id}")
async def checkout(order_id: str, request: Request):
    try:
        payment = await peyamesh.start_payment(request, amount=50_000, order_id=order_id)
    except PeyameshError as exc:
        return HTMLResponse(exc.message, status_code=400)
    return payment.redirect()


@app.get("/result", response_class=HTMLResponse)
async def result(request: Request) -> str:
    payment = await peyamesh.get_returned_payment(request)
    if payment and payment.is_paid:
        return f"پرداخت موفق بود. شماره پیگیری: {payment.ref_id}"
    if payment and payment.is_pending:
        return "نتیجه‌ی پرداخت در حال بررسی است."
    return "پرداخت انجام نشد."
