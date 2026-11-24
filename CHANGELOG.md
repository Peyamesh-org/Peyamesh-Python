# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-24

### Added
- `Peyamesh` (sync) and `AsyncPeyamesh` (async) API clients: create, verify, get and list unverified
  payments; create and get refunds.
- Automatic `Idempotency-Key`, `X-Request-ID`, and safe retries with exponential backoff for network
  errors, timeouts, `429`, `500`, `502`, `504` and retryable `503` answers.
- Typed exception hierarchy carrying the server's error `code`, Persian `message`, `details` and `request_id`.
- Webhook signature verification (`peyamesh.webhooks`) with timestamp tolerance and secret rotation.
- Django integration (`peyamesh.contrib.django`): `PeyameshPayment` model and admin, callback and webhook
  views, `payment_verified` / `payment_failed` / `payment_refunded` / `webhook_received` signals,
  `peyamesh_reconcile` management command, refunds, system checks.
- FastAPI integration (`peyamesh.contrib.fastapi`): router with callback and webhook endpoints, hooks,
  `SQLAlchemyStore` / `MemoryStore` / `PaymentStore` protocol, reconciliation and refunds.
- Guaranteed order delivery: the `payment_verified` signal / `on_payment_verified` hooks are run again by
  the reconciler until they all succeed (up to 3 days after verification), tracked by `notified_at`.
- Reconciliation prunes processed webhook events after `WEBHOOK_EVENT_RETENTION_DAYS` /
  `webhook_event_retention_days` (default 30).
- `PeyameshPayment.objects.unnotified()`, `PeyameshFastAPI.get_payments_for_order()`, and Django system
  checks for an unresolvable callback URL and an invalid retention setting.
- The FastAPI webhook endpoint rejects bodies larger than 256 KB before reading them whole.
- `peyamesh.testing.FakeGateway`: an in-memory imitation of the API for tests without network.
