"""SQLAlchemy Core implementations of the repository ports.

Each repository wraps an ``AsyncConnection``; the caller owns the transaction, so
several repositories can take part in one atomic unit of work.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from rates.adapters.db.tables import (
    audit_events,
    calendar_coverage,
    currency_pairs,
    non_publication_days,
    policies,
    rate_observations,
    refresh_status,
)
from rates.adapters.db.types import insert_ignore
from rates.domain.calendar import CalendarView
from rates.domain.models import AuditEvent, CurrencyPair, Policy, RateObservation, RefreshStatus


@dataclass(frozen=True)
class Correction:
    """BoI restated a rate without changing its ``lastUpdate``."""

    pair: str
    source_last_update: datetime
    previous_rate: Decimal
    previous_unit: int
    new_rate: Decimal
    new_unit: int
    revision: int


@dataclass(frozen=True)
class StoreResult:
    inserted: list[RateObservation]
    corrections: list[Correction]


class SqlPairRepository:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def is_enabled(self, base: str, quote: str) -> bool:
        stmt = select(currency_pairs.c.enabled).where(
            currency_pairs.c.base_currency == base,
            currency_pairs.c.quote_currency == quote,
        )
        return bool((await self._conn.execute(stmt)).scalar_one_or_none())

    async def get_enabled(self, base: str, quote: str) -> bool | None:
        """The pair's enabled flag, or None if the pair is not configured."""
        stmt = select(currency_pairs.c.enabled).where(
            currency_pairs.c.base_currency == base,
            currency_pairs.c.quote_currency == quote,
        )
        return (await self._conn.execute(stmt)).scalar_one_or_none()

    async def set_enabled(self, base: str, quote: str, enabled: bool) -> None:
        await self._conn.execute(
            update(currency_pairs)
            .where(currency_pairs.c.base_currency == base, currency_pairs.c.quote_currency == quote)
            .values(enabled=enabled)
        )

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
                rate_observations.c.revision.desc(),
            )
            .limit(1)
        )
        row = (await self._conn.execute(stmt)).mappings().first()
        return _to_observation(row) if row else None

    async def store_batch(self, observations: Sequence[RateObservation]) -> StoreResult:
        """Store each observation unless it repeats the latest stored revision.

        For each observation's ``(pair, source_last_update)``:
        - nothing stored yet -> insert as revision 0;
        - latest revision has the same source rate and unit -> no-op (idempotent);
        - otherwise BoI restated the value -> insert as the next revision (a correction).

        Comparing with the *latest* revision means a value that flips back (A -> B -> A)
        becomes current again. Runs inside the caller's transaction.
        """
        inserted: list[RateObservation] = []
        corrections: list[Correction] = []
        stmt = insert_ignore(
            self._conn.dialect.name,
            rate_observations,
            ["base_currency", "quote_currency", "source_last_update", "revision"],
        )
        for obs in observations:
            latest = await self._latest_revision(obs)
            correction: Correction | None = None
            if latest is None:
                revision = 0
            elif latest.source_rate == obs.source_rate and latest.source_unit == obs.source_unit:
                continue
            else:
                revision = latest.revision + 1
                correction = Correction(
                    pair=f"{obs.base_currency}/{obs.quote_currency}",
                    source_last_update=obs.source_last_update,
                    previous_rate=latest.source_rate,
                    previous_unit=latest.source_unit,
                    new_rate=obs.source_rate,
                    new_unit=obs.source_unit,
                    revision=revision,
                )
            obs = replace(obs, revision=revision)
            result = await self._conn.execute(stmt.values(**_from_observation(obs)))
            if result.rowcount == 1:
                inserted.append(obs)
                if correction is not None:
                    corrections.append(correction)
        return StoreResult(inserted, corrections)

    async def _latest_revision(self, obs: RateObservation) -> RateObservation | None:
        stmt = (
            select(rate_observations)
            .where(
                rate_observations.c.base_currency == obs.base_currency,
                rate_observations.c.quote_currency == obs.quote_currency,
                rate_observations.c.source_last_update == obs.source_last_update,
            )
            .order_by(rate_observations.c.revision.desc())
            .limit(1)
        )
        row = (await self._conn.execute(stmt)).mappings().first()
        return _to_observation(row) if row else None

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

    async def get(self, day: date) -> dict[str, object] | None:
        stmt = select(non_publication_days).where(non_publication_days.c.day == day)
        row = (await self._conn.execute(stmt)).mappings().first()
        return dict(row) if row else None

    async def delete(self, day: date) -> None:
        await self._conn.execute(delete(non_publication_days).where(non_publication_days.c.day == day))

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

    async def active_row(self) -> dict[str, object] | None:
        rows = (await self._conn.execute(select(policies).where(policies.c.active.is_(True)))).mappings().all()
        return dict(rows[0]) if len(rows) == 1 else None

    async def max_version(self) -> int:
        return (await self._conn.execute(select(func.max(policies.c.version)))).scalar_one() or 0

    async def deactivate(self, version: int) -> None:
        await self._conn.execute(update(policies).where(policies.c.version == version).values(active=False))

    async def insert_active(self, values: dict[str, object]) -> None:
        await self._conn.execute(policies.insert().values(**values, active=True))

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
        revision=row["revision"],
        source_change=row["source_change"],
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
        "revision": obs.revision,
        "source_change": obs.source_change,
    }


class SqlRefreshStatusRepository:
    """The single ``refresh_status`` row (id = 1), seeded by migration 0003."""

    _ROW = refresh_status.c.id == 1

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def get(self) -> RefreshStatus:
        row = (await self._conn.execute(select(refresh_status).where(self._ROW))).mappings().first()
        if row is None:
            return RefreshStatus()
        return RefreshStatus(
            last_attempt_at=row["last_attempt_at"],
            last_attempt_outcome=row["last_attempt_outcome"],
            last_success_at=row["last_success_at"],
            consecutive_failures=row["consecutive_failures"],
        )

    async def record_success(self, at: datetime) -> bool:
        """Returns False if the row is missing (nothing was written)."""
        return await self._update(
            last_attempt_at=at,
            last_attempt_outcome="success",
            last_success_at=at,
            consecutive_failures=0,
        )

    async def record_failure(self, at: datetime, outcome: str, consecutive_failures: int) -> bool:
        """Records a failed attempt; ``last_success_at`` is left as it was."""
        return await self._update(
            last_attempt_at=at,
            last_attempt_outcome=outcome,
            consecutive_failures=consecutive_failures,
        )

    async def _update(self, **values) -> bool:
        result = await self._conn.execute(update(refresh_status).where(self._ROW).values(**values))
        return result.rowcount == 1
