from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from rates.adapters.db.repos import (
    SqlCalendarRepository,
    SqlPairRepository,
    SqlPolicyRepository,
    SqlRateRepository,
)
from rates.adapters.db.tables import currency_pairs, policies
from rates.domain.models import RateObservation


def obs(rate: str, published: date, source_ts: datetime, base: str = "USD") -> RateObservation:
    return RateObservation(
        id=uuid4(),
        base_currency=base,
        quote_currency="ILS",
        rate=Decimal(rate),
        source_rate=Decimal(rate),
        source_unit=1,
        publication_date=published,
        source_last_update=source_ts,
        retrieved_at=source_ts,
    )


async def test_pairs(engine):
    async with engine.begin() as conn:
        repo = SqlPairRepository(conn)
        assert await repo.is_enabled("USD", "ILS")
        assert not await repo.is_enabled("ILS", "USD")

        await conn.execute(update(currency_pairs).where(currency_pairs.c.base_currency == "GBP").values(enabled=False))
        assert not await repo.is_enabled("GBP", "ILS")
        assert [p.base for p in await repo.enabled_pairs()] == ["EUR", "USD"]


async def test_latest_for_pair_orders_by_date_then_source_timestamp(engine):
    older = obs("3.300", date(2026, 11, 20), datetime(2026, 11, 20, 13, 20, tzinfo=UTC))
    first = obs("3.343", date(2026, 11, 23), datetime(2026, 11, 23, 13, 21, tzinfo=UTC))
    corrected = obs("3.341", date(2026, 11, 23), datetime(2026, 11, 23, 14, 5, tzinfo=UTC))

    async with engine.begin() as conn:
        repo = SqlRateRepository(conn)
        assert await repo.latest_for_pair("USD", "ILS") is None
        await repo.insert_many([corrected, older, first])

    async with engine.connect() as conn:
        latest = await SqlRateRepository(conn).latest_for_pair("USD", "ILS")
    assert latest is not None
    assert latest.id == corrected.id
    assert latest.rate == Decimal("3.341")


async def test_insert_many_ignores_duplicates(engine):
    ts = datetime(2026, 11, 23, 13, 21, 10, 710489, tzinfo=UTC)
    async with engine.begin() as conn:
        repo = SqlRateRepository(conn)
        assert len(await repo.insert_many([obs("3.343", date(2026, 11, 23), ts)])) == 1
        # Same pair + source timestamp, new id: ignored.
        assert await repo.insert_many([obs("3.343", date(2026, 11, 23), ts)]) == []
        assert await repo.latest_retrieved_at() == ts


async def test_no_active_policy_returns_none(engine):
    async with engine.begin() as conn:
        assert (await SqlPolicyRepository(conn).active()) is not None
        await conn.execute(update(policies).values(active=False))
        assert await SqlPolicyRepository(conn).active() is None


async def test_single_active_policy_enforced(engine):
    async with engine.begin() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                policies.insert().values(
                    version=2, grace_minutes=30, max_age_days=5,
                    weekday_publish_time=datetime(2026, 1, 1, 15, 15).time(),
                    friday_publish_time=datetime(2026, 1, 1, 12, 15).time(),
                    timezone="Asia/Jerusalem", active=True,
                    created_at=datetime(2026, 11, 24, tzinfo=UTC),
                )
            )


async def test_calendar_view(engine):
    async with engine.connect() as conn:
        view = await SqlCalendarRepository(conn).load_view()
        assert await SqlCalendarRepository(conn).exists(date(2026, 10, 27))
    assert view.covered_years == {2026, 2027}
    assert date(2026, 10, 27) in view.non_publication_days
    assert not view.is_business_day(date(2026, 10, 27))
