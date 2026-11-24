from __future__ import annotations

import pytest

from peyamesh import APIConnectionError, PaymentNotPaidError, PaymentStatus
from peyamesh.testing import FakeGateway

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr("peyamesh.async_client.anyio.sleep", instant)


async def test_async_full_flow():
    gateway = FakeGateway()
    async with gateway.async_client(currency="IRT") as client:
        payment = await client.create_payment(10_000, "https://shop.test/back", order_id="9")
        assert payment.status == PaymentStatus.CREATED
        with pytest.raises(PaymentNotPaidError):
            await client.verify_payment(payment.authority, 10_000)
        gateway.pay(payment.authority)
        assert [p.authority for p in await client.list_unverified_payments()] == [payment.authority]
        callback, result = await client.verify_callback(gateway.callback_params(payment.authority), 10_000)
        assert callback.is_ok and result.code == 100
        assert (await client.get_payment(payment.authority)).is_verified
        refund = await client.create_refund(payment.authority, amount=10_000)
        assert (await client.get_refund(refund.id)).amount_irr == 100_000


async def test_async_retries_and_errors():
    gateway = FakeGateway()
    client = gateway.async_client(max_retries=2)
    gateway.network_error_next(times=2)
    payment = await client.create_payment(10_000, "https://shop.test/back")
    assert len(gateway.payments) == 1 and payment.authority
    gateway.network_error_next(times=3)
    with pytest.raises(APIConnectionError):
        await client.get_payment(payment.authority)
    await client.aclose()
