"""Persistence of payment records for the FastAPI integration.

* :class:`SQLAlchemyStore` — production store on a SQLAlchemy async engine (tested on PostgreSQL and SQLite).
* :class:`MemoryStore` — in-process store for tests and demos (data is lost on restart).

Implement :class:`PaymentStore` to keep records anywhere else (MongoDB, Redis, your own ORM models...).
The only hard requirements are that :meth:`PaymentStore.transition` and :meth:`PaymentStore.claim` are
atomic compare-and-set operations.
"""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Collection, Mapping, Sequence
from dataclasses import fields as dc_fields
from typing import Any, Optional, Protocol, Union, runtime_checkable

from .records import PaymentRecord, utcnow

_RECORD_FIELDS = tuple(f.name for f in dc_fields(PaymentRecord))
_DATETIME_FIELDS = ("expires_at", "verified_at", "notified_at", "last_synced_at", "created_at", "updated_at")


@runtime_checkable
class PaymentStore(Protocol):
    async def setup(self) -> None:
        """Prepare the storage (create tables...). Called once, lazily or from the app lifespan."""

    async def close(self) -> None: ...

    async def insert(self, record: PaymentRecord) -> None: ...

    async def get(self, record_id: str) -> Optional[PaymentRecord]: ...

    async def get_by_authority(self, authority: str) -> Optional[PaymentRecord]: ...

    async def list_by_authorities(self, authorities: Sequence[str]) -> list[PaymentRecord]: ...

    async def list_by_order(self, order_id: str) -> list[PaymentRecord]:
        """Every record of ``order_id``, newest first."""

    async def list_stale(
        self, *, statuses: Collection[str], created_before: dt.datetime, limit: int
    ) -> list[PaymentRecord]:
        """Records in ``statuses`` created before ``created_before`` that have an authority, least recently
        synced first."""

    async def list_unnotified(
        self, *, statuses: Collection[str], updated_before: dt.datetime, verified_after: dt.datetime, limit: int
    ) -> list[PaymentRecord]:
        """Records in ``statuses`` with no ``notified_at``, updated before ``updated_before`` and verified after
        ``verified_after``, least recently updated first."""

    async def claim(self, record_id: str, *, updated_before: dt.datetime) -> bool:
        """Atomically bump ``updated_at`` **only if** the record has no ``notified_at`` and was last updated
        before ``updated_before``. Returns whether it did — so that one caller only runs the hooks again."""

    async def mark_notified(self, record_id: str, when: dt.datetime) -> None:
        """Set ``notified_at`` (without touching ``updated_at``) if it is still empty."""

    async def update(self, record_id: str, values: Mapping[str, Any]) -> None: ...

    async def transition(
        self,
        record_id: str,
        to_status: str,
        from_statuses: Collection[str],
        values: Mapping[str, Any],
        *,
        refunded_below: Optional[int] = None,
    ) -> bool:
        """Atomically set ``status=to_status`` (and ``values``) **only if** the record's status is in
        ``from_statuses`` (and, if given, its ``refunded_irr`` is below ``refunded_below``).
        Returns whether the row was updated."""

    async def add_event(self, event_id: str, type: str, livemode: bool, payload: Mapping[str, Any]) -> bool:
        """Remember a processed webhook event; ``False`` if it was already recorded."""

    async def has_event(self, event_id: str) -> bool: ...

    async def prune_events(self, *, received_before: dt.datetime) -> int:
        """Forget processed events received before ``received_before``; returns how many were deleted."""


# --------------------------------------------------------------------------- memory
class MemoryStore:
    """Keeps records in a dict. For tests, demos and single-process prototypes only."""

    def __init__(self) -> None:
        self.records: dict[str, PaymentRecord] = {}
        self.events: dict[str, dict[str, Any]] = {}

    async def setup(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def insert(self, record: PaymentRecord) -> None:
        if record.id in self.records:
            raise ValueError(f"duplicate record id {record.id}")
        if record.authority and await self.get_by_authority(record.authority):
            raise ValueError(f"duplicate authority {record.authority}")
        self.records[record.id] = copy.deepcopy(record)

    async def get(self, record_id: str) -> Optional[PaymentRecord]:
        record = self.records.get(record_id)
        return copy.deepcopy(record) if record else None

    async def get_by_authority(self, authority: str) -> Optional[PaymentRecord]:
        for record in self.records.values():
            if record.authority == authority:
                return copy.deepcopy(record)
        return None

    async def list_by_authorities(self, authorities: Sequence[str]) -> list[PaymentRecord]:
        wanted = set(authorities)
        return [copy.deepcopy(r) for r in self.records.values() if r.authority in wanted]

    async def list_by_order(self, order_id: str) -> list[PaymentRecord]:
        rows = [r for r in self.records.values() if r.order_id == str(order_id)]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return [copy.deepcopy(r) for r in rows]

    async def list_unnotified(
        self, *, statuses: Collection[str], updated_before: dt.datetime, verified_after: dt.datetime, limit: int
    ) -> list[PaymentRecord]:
        rows = [
            r
            for r in self.records.values()
            if r.status in statuses
            and r.notified_at is None
            and r.updated_at <= updated_before
            and r.verified_at is not None
            and r.verified_at >= verified_after
        ]
        rows.sort(key=lambda r: r.updated_at)
        return [copy.deepcopy(r) for r in rows[: max(0, limit)]]

    async def claim(self, record_id: str, *, updated_before: dt.datetime) -> bool:
        record = self.records.get(record_id)
        if record is None or record.notified_at is not None or record.updated_at > updated_before:
            return False
        record.updated_at = utcnow()
        return True

    async def mark_notified(self, record_id: str, when: dt.datetime) -> None:
        record = self.records.get(record_id)
        if record is not None and record.notified_at is None:
            record.notified_at = when

    async def list_stale(
        self, *, statuses: Collection[str], created_before: dt.datetime, limit: int
    ) -> list[PaymentRecord]:
        floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        rows = [
            r for r in self.records.values() if r.status in statuses and r.authority and r.created_at <= created_before
        ]
        rows.sort(key=lambda r: (r.last_synced_at is not None, r.last_synced_at or floor, r.created_at))
        return [copy.deepcopy(r) for r in rows[: max(0, limit)]]

    async def update(self, record_id: str, values: Mapping[str, Any]) -> None:
        record = self.records[record_id]
        for name, value in values.items():
            setattr(record, name, copy.deepcopy(value))
        record.updated_at = utcnow()

    async def transition(
        self,
        record_id: str,
        to_status: str,
        from_statuses: Collection[str],
        values: Mapping[str, Any],
        *,
        refunded_below: Optional[int] = None,
    ) -> bool:
        record = self.records.get(record_id)
        if record is None or record.status not in from_statuses:
            return False
        if refunded_below is not None and record.refunded_irr >= refunded_below:
            return False
        await self.update(record_id, {**values, "status": to_status})
        return True

    async def add_event(self, event_id: str, type: str, livemode: bool, payload: Mapping[str, Any]) -> bool:
        if event_id in self.events:
            return False
        self.events[event_id] = {"type": type, "livemode": livemode, "payload": dict(payload), "received_at": utcnow()}
        return True

    async def has_event(self, event_id: str) -> bool:
        return event_id in self.events

    async def prune_events(self, *, received_before: dt.datetime) -> int:
        old = [k for k, v in self.events.items() if v["received_at"] < received_before]
        for key in old:
            del self.events[key]
        return len(old)


# --------------------------------------------------------------------------- SQLAlchemy
class SQLAlchemyStore:
    """Stores records in two tables (``<prefix>payments`` and ``<prefix>webhook_events``).

    ::

        store = SQLAlchemyStore("postgresql+asyncpg://user:pass@localhost/shop")
        # or reuse your engine: SQLAlchemyStore(engine)

    Tables are created on startup when ``create_tables=True`` (the default). With Alembic, pass your own
    ``metadata`` (``Base.metadata``) and ``create_tables=False`` so the tables are part of your migrations.
    """

    def __init__(
        self,
        engine: Union[str, Any],
        *,
        table_prefix: str = "peyamesh_",
        create_tables: bool = True,
        metadata: Any = None,
        engine_options: Optional[Mapping[str, Any]] = None,
    ) -> None:
        try:
            import sqlalchemy as sa
            from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
        except ImportError as exc:  # pragma: no cover
            raise ImportError("SQLAlchemyStore needs SQLAlchemy 2: pip install 'peyamesh[fastapi]'") from exc

        self._sa = sa
        if isinstance(engine, str):
            self.engine = create_async_engine(engine, **dict(engine_options or {}))
            self._owns_engine = True
        elif isinstance(engine, AsyncEngine):
            self.engine = engine
            self._owns_engine = False
        else:
            raise TypeError(
                "SQLAlchemyStore needs an AsyncEngine or a database URL with an async driver "
                "(e.g. 'postgresql+asyncpg://...', 'sqlite+aiosqlite:///shop.db')."
            )
        self.create_tables = create_tables
        self.metadata = metadata if metadata is not None else sa.MetaData()
        tz = sa.DateTime(timezone=True)
        self.payments = sa.Table(
            f"{table_prefix}payments",
            self.metadata,
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("authority", sa.String(64), unique=True, nullable=True),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("remote_status", sa.String(24), nullable=False, default=""),
            sa.Column("livemode", sa.Boolean, nullable=True),
            sa.Column("amount", sa.BigInteger, nullable=False),
            sa.Column("currency", sa.String(3), nullable=False, default=""),
            sa.Column("amount_irr", sa.BigInteger, nullable=True),
            sa.Column("fee_irr", sa.BigInteger, nullable=False, default=0),
            sa.Column("refunded_irr", sa.BigInteger, nullable=False, default=0),
            sa.Column("order_id", sa.String(100), nullable=False, default="", index=True),
            sa.Column("description", sa.String(500), nullable=False, default=""),
            sa.Column("mobile", sa.String(20), nullable=False, default=""),
            sa.Column("email", sa.String(254), nullable=False, default=""),
            sa.Column("metadata", sa.JSON, nullable=False),
            sa.Column("payment_url", sa.String(2048), nullable=False, default=""),
            sa.Column("callback_url", sa.String(2048), nullable=False, default=""),
            sa.Column("return_url", sa.String(2048), nullable=False, default=""),
            sa.Column("ref_id", sa.String(32), nullable=False, default="", index=True),
            sa.Column("card_pan", sa.String(32), nullable=False, default=""),
            sa.Column("card_hash", sa.String(128), nullable=False, default=""),
            sa.Column("failure_code", sa.String(64), nullable=False, default=""),
            sa.Column("failure_message", sa.String(500), nullable=False, default=""),
            sa.Column("expires_at", tz, nullable=True),
            sa.Column("verified_at", tz, nullable=True),
            sa.Column("notified_at", tz, nullable=True),
            sa.Column("last_synced_at", tz, nullable=True),
            sa.Column("created_at", tz, nullable=False),
            sa.Column("updated_at", tz, nullable=False),
            sa.Index(f"ix_{table_prefix}payments_status_created", "status", "created_at"),
        )
        self.events = sa.Table(
            f"{table_prefix}webhook_events",
            self.metadata,
            sa.Column("event_id", sa.String(64), primary_key=True),
            sa.Column("type", sa.String(64), nullable=False),
            sa.Column("livemode", sa.Boolean, nullable=False),
            sa.Column("payload", sa.JSON, nullable=False),
            sa.Column("received_at", tz, nullable=False, index=True),
        )

    # ------------------------------------------------------------------ lifecycle
    async def setup(self) -> None:
        if self.create_tables:
            async with self.engine.begin() as conn:
                await conn.run_sync(self.metadata.create_all, tables=[self.payments, self.events])

    async def close(self) -> None:
        if self._owns_engine:
            await self.engine.dispose()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _aware(value: Any) -> Any:
        if isinstance(value, dt.datetime) and value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)  # SQLite drops the offset; we always store UTC
        return value

    @staticmethod
    def _utc(value: Any) -> Any:
        if isinstance(value, dt.datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=dt.timezone.utc)
            return value.astimezone(dt.timezone.utc)
        return value

    def _record(self, row: Any) -> PaymentRecord:
        data = dict(row._mapping)
        for name in _DATETIME_FIELDS:
            data[name] = self._aware(data.get(name))
        data["metadata"] = data.get("metadata") or {}
        return PaymentRecord(**{k: v for k, v in data.items() if k in _RECORD_FIELDS})

    def _values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in values.items() if k in _RECORD_FIELDS}
        for name in _DATETIME_FIELDS:
            if name in out:
                out[name] = self._utc(out[name])
        return out

    async def _one(self, stmt: Any) -> Optional[PaymentRecord]:
        async with self.engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        return self._record(row) if row is not None else None

    async def _many(self, stmt: Any) -> list[PaymentRecord]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [self._record(r) for r in rows]

    # ------------------------------------------------------------------ records
    async def insert(self, record: PaymentRecord) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(self.payments.insert().values(**self._values(record.to_dict())))

    async def get(self, record_id: str) -> Optional[PaymentRecord]:
        return await self._one(self._sa.select(self.payments).where(self.payments.c.id == record_id))

    async def get_by_authority(self, authority: str) -> Optional[PaymentRecord]:
        return await self._one(self._sa.select(self.payments).where(self.payments.c.authority == authority))

    async def list_by_authorities(self, authorities: Sequence[str]) -> list[PaymentRecord]:
        if not authorities:
            return []
        return await self._many(self._sa.select(self.payments).where(self.payments.c.authority.in_(list(authorities))))

    async def list_by_order(self, order_id: str) -> list[PaymentRecord]:
        t = self.payments
        return await self._many(self._sa.select(t).where(t.c.order_id == str(order_id)).order_by(t.c.created_at.desc()))

    async def list_unnotified(
        self, *, statuses: Collection[str], updated_before: dt.datetime, verified_after: dt.datetime, limit: int
    ) -> list[PaymentRecord]:
        t = self.payments
        stmt = (
            self._sa.select(t)
            .where(
                t.c.status.in_(list(statuses)),
                t.c.notified_at.is_(None),
                t.c.updated_at <= self._utc(updated_before),
                t.c.verified_at >= self._utc(verified_after),
            )
            .order_by(t.c.updated_at)
            .limit(max(0, limit))
        )
        return await self._many(stmt)

    async def claim(self, record_id: str, *, updated_before: dt.datetime) -> bool:
        t = self.payments
        async with self.engine.begin() as conn:
            result = await conn.execute(
                t.update()
                .where(t.c.id == record_id, t.c.notified_at.is_(None), t.c.updated_at <= self._utc(updated_before))
                .values(updated_at=utcnow())
            )
        return bool(result.rowcount == 1)

    async def mark_notified(self, record_id: str, when: dt.datetime) -> None:
        t = self.payments
        async with self.engine.begin() as conn:
            await conn.execute(
                t.update().where(t.c.id == record_id, t.c.notified_at.is_(None)).values(notified_at=self._utc(when))
            )

    async def list_stale(
        self, *, statuses: Collection[str], created_before: dt.datetime, limit: int
    ) -> list[PaymentRecord]:
        t = self.payments
        stmt = (
            self._sa.select(t)
            .where(
                t.c.status.in_(list(statuses)), t.c.authority.is_not(None), t.c.created_at <= self._utc(created_before)
            )
            .order_by(t.c.last_synced_at.is_not(None), t.c.last_synced_at, t.c.created_at)
            .limit(max(0, limit))
        )
        return await self._many(stmt)

    async def update(self, record_id: str, values: Mapping[str, Any]) -> None:
        data = self._values({**values, "updated_at": utcnow()})
        async with self.engine.begin() as conn:
            await conn.execute(self.payments.update().where(self.payments.c.id == record_id).values(**data))

    async def transition(
        self,
        record_id: str,
        to_status: str,
        from_statuses: Collection[str],
        values: Mapping[str, Any],
        *,
        refunded_below: Optional[int] = None,
    ) -> bool:
        t = self.payments
        conditions = [t.c.id == record_id, t.c.status.in_(list(from_statuses))]
        if refunded_below is not None:
            conditions.append(t.c.refunded_irr < refunded_below)
        data = self._values({**values, "status": to_status, "updated_at": utcnow()})
        async with self.engine.begin() as conn:
            result = await conn.execute(t.update().where(*conditions).values(**data))
        return bool(result.rowcount == 1)

    # ------------------------------------------------------------------ events
    async def add_event(self, event_id: str, type: str, livemode: bool, payload: Mapping[str, Any]) -> bool:
        try:
            async with self.engine.begin() as conn:
                await conn.execute(
                    self.events.insert().values(
                        event_id=event_id,
                        type=type[:64],
                        livemode=livemode,
                        payload=dict(payload),
                        received_at=utcnow(),
                    )
                )
        except self._sa.exc.IntegrityError:
            return False
        return True

    async def has_event(self, event_id: str) -> bool:
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(self._sa.select(self.events.c.event_id).where(self.events.c.event_id == event_id))
            ).first()
        return row is not None

    async def prune_events(self, *, received_before: dt.datetime) -> int:
        e = self.events
        async with self.engine.begin() as conn:
            result = await conn.execute(e.delete().where(e.c.received_at < self._utc(received_before)))
        return int(result.rowcount or 0)


__all__ = ["MemoryStore", "PaymentStore", "SQLAlchemyStore"]
