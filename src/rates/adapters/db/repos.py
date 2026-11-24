"""SQLAlchemy Core implementations of the repository ports.

Each repository wraps an ``AsyncConnection``; the caller owns the transaction, so
several repositories can take part in one atomic unit of work.
"""

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from rates.adapters.db.tables import (
    audit_events,
    calendar_coverage,
    currency_pairs,
    non_publication_days,
    policies,
    rate_observations,
)
from rates.adapters.db.types import insert_ignore
from rates.domain.calendar import CalendarView
from rates.domain.models import AuditEvent, CurrencyPair, Policy, RateObservation


class SqlPairRepository:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def is_enabled(self, base: str, quote: str) -> bool:
        stmt = select(currency_pairs.c.enabled).where(
            currency_pairs.c.base_currency == base,
            currency_pairs.c.quote_currency == quote,
        )
        return bool((await self._conn.execute(stmt)).scalar_one_or_none())

    async def enabled_pairs(self) -> list[CurrencyPair]:
        stmt = (
            select(currency_pairs)
            .where(currency_pairs.c.enabled.is_(True))
            .order_by(currency_pairs.c.base_currency, currency_pairs.c.quote_currency)
        )
        rows = (await self._conn.execute(stmt)).mappings()
        return [CurrencyPair(r["base_currency"], r["quote_currency"], r["source_key"]) for r in rows]


class SqlRateRepository:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def latest_for_pair(self, base: str, quote: str) -> RateObservation | None:
        stmt = (
            select(rate_observations)
            .where(
                rate_observations.c.base_currency == base,
                rate_observations.c.quote_currency == quote,
            )
            .order_by(
                rate_observations.c.publication_date.desc(),
                rate_observations.c.source_last_update.desc(),
            )
            .limit(1)
        )
        row = (await self._conn.execute(stmt)).mappings().first()
        return _to_observation(row) if row else None

    async def insert_many(self, observations: Sequence[RateObservation]) -> list[RateObservation]:
        """Insert observations, ignoring ones already stored. Returns those actually inserted."""
        stmt = insert_ignore(
            self._conn.dialect.name,
            rate_observations,
            ["base_currency", "quote_currency", "source_last_update"],
        )
        inserted: list[RateObservation] = []
        for obs in observations:
            result = await self._conn.execute(stmt.values(**_from_observation(obs)))
            if result.rowcount == 1:
                inserted.append(obs)
        return inserted

    async def latest_retrieved_at(self) -> datetime | None:
        stmt = select(func.max(rate_observations.c.retrieved_at))
        # func.max() keeps the column type, so UtcDateTime decoding applies.
        return (await self._conn.execute(stmt)).scalar_one_or_none()


class SqlCalendarRepository:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def load_view(self) -> CalendarView:
        days = (await self._conn.execute(select(non_publication_days.c.day))).scalars().all()
        years = (await self._conn.execute(select(calendar_coverage.c.year))).scalars().all()
        return CalendarView(frozenset(days), frozenset(years))

    async def exists(self, day: date) -> bool:
        stmt = select(non_publication_days.c.day).where(non_publication_days.c.day == day)
        return (await self._conn.execute(stmt)).first() is not None

    async def add(
        self, day: date, reason: str, origin: str, created_by: str, created_at: datetime
    ) -> None:
        await self._conn.execute(
            non_publication_days.insert().values(
                day=day, reason=reason, origin=origin, created_by=created_by, created_at=created_at
            )
        )


class SqlPolicyRepository:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def active(self) -> Policy | None:
        stmt = select(policies).where(policies.c.active.is_(True))
        rows = (await self._conn.execute(stmt)).mappings().all()
        if len(rows) != 1:
            # Zero or (should the constraint ever be bypassed) several: not evaluable.
            return None
        r = rows[0]
        return Policy(
            version=r["version"],
            grace_minutes=r["grace_minutes"],
            max_age_days=r["max_age_days"],
            weekday_publish_time=r["weekday_publish_time"],
            friday_publish_time=r["friday_publish_time"],
            timezone=r["timezone"],
        )


class SqlAuditRepository:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def record(self, event: AuditEvent) -> None:
        await self._conn.execute(
            audit_events.insert().values(
                id=event.id,
                occurred_at=event.occurred_at,
                actor=event.actor,
                event_type=event.event_type,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                before=event.before,
                after=event.after,
                reason=event.reason,
            )
        )


def _to_observation(row: RowMapping) -> RateObservation:
    return RateObservation(
        id=row["id"],
        base_currency=row["base_currency"],
        quote_currency=row["quote_currency"],
        rate=row["rate"],
        source_rate=row["source_rate"],
        source_unit=row["source_unit"],
        publication_date=row["publication_date"],
        source_last_update=row["source_last_update"],
        retrieved_at=row["retrieved_at"],
    )


def _from_observation(obs: RateObservation) -> dict[str, object]:
    return {
        "id": obs.id,
        "base_currency": obs.base_currency,
        "quote_currency": obs.quote_currency,
        "rate": obs.rate,
        "source_rate": obs.source_rate,
        "source_unit": obs.source_unit,
        "publication_date": obs.publication_date,
        "source_last_update": obs.source_last_update,
        "retrieved_at": obs.retrieved_at,
    }
