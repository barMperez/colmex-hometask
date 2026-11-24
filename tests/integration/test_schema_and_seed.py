from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import func, inspect, select, text

from rates.adapters.db.session import create_engine
from rates.adapters.db.tables import (
    calendar_coverage,
    currency_pairs,
    non_publication_days,
    policies,
    rate_observations,
    refresh_status,
)
from tests.conftest import alembic_config, is_postgres


async def test_sqlite_pragmas(engine):
    if is_postgres():
        pytest.skip("SQLite-specific")
    async with engine.connect() as conn:
        assert (await conn.execute(text("PRAGMA journal_mode"))).scalar_one() == "wal"
        assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
        assert (await conn.execute(text("PRAGMA busy_timeout"))).scalar_one() == 5000


async def _table_names(url: str) -> set[str]:
    eng = create_engine(url)
    try:
        async with eng.connect() as conn:
            return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    finally:
        await eng.dispose()


async def test_upgrade_downgrade_upgrade(migrated_url):
    expected = {
        "currency_pairs", "rate_observations", "non_publication_days",
        "calendar_coverage", "policies", "audit_events", "refresh_status",
    }
    assert expected <= await _table_names(migrated_url)

    await _run_sync_alembic(lambda: command.downgrade(alembic_config(migrated_url), "base"))
    assert not (expected & await _table_names(migrated_url))

    await _run_sync_alembic(lambda: command.upgrade(alembic_config(migrated_url), "head"))
    assert expected <= await _table_names(migrated_url)


async def _run_sync_alembic(fn):
    # env.py uses asyncio.run(), which cannot nest inside the test's event loop.
    import asyncio

    await asyncio.to_thread(fn)


async def test_seed_data(engine):
    async with engine.connect() as conn:
        pairs = (await conn.execute(
            select(currency_pairs.c.base_currency, currency_pairs.c.quote_currency, currency_pairs.c.enabled)
            .order_by(currency_pairs.c.base_currency)
        )).all()
        active = (await conn.execute(select(policies).where(policies.c.active.is_(True)))).mappings().all()
        days_2026 = (await conn.execute(
            select(func.count()).where(non_publication_days.c.day.between(date(2026, 1, 1), date(2026, 12, 31)))
        )).scalar_one()
        days_2027 = (await conn.execute(
            select(func.count()).where(non_publication_days.c.day.between(date(2027, 1, 1), date(2027, 12, 31)))
        )).scalar_one()
        years = (await conn.execute(select(calendar_coverage.c.year).order_by(calendar_coverage.c.year))).scalars().all()
        status = (await conn.execute(select(refresh_status))).mappings().all()

    assert [tuple(p) for p in pairs] == [("EUR", "ILS", True), ("GBP", "ILS", True), ("USD", "ILS", True)]
    assert len(active) == 1
    assert active[0]["version"] == 1
    assert active[0]["grace_minutes"] == 45
    assert active[0]["max_age_days"] == 7
    assert active[0]["weekday_publish_time"] == time(15, 15)
    assert active[0]["friday_publish_time"] == time(12, 15)
    assert days_2026 == 16
    assert days_2027 == 13
    assert years == [2026, 2027]
    assert [dict(r) for r in status] == [
        {
            "id": 1,
            "last_attempt_at": None,
            "last_attempt_outcome": None,
            "last_success_at": None,
            "consecutive_failures": 0,
        }
    ]


async def _columns_and_uniques(url: str):
    eng = create_engine(url)
    try:
        async with eng.connect() as conn:
            def inspect_it(c):
                insp = inspect(c)
                cols = {col["name"]: col for col in insp.get_columns("rate_observations")}
                uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints("rate_observations")}
                return cols, uniques
            return await conn.run_sync(inspect_it)
    finally:
        await eng.dispose()


async def test_rate_observations_have_revisions(migrated_url):
    cols, uniques = await _columns_and_uniques(migrated_url)
    assert "revision" in cols and cols["revision"]["nullable"] is False
    assert ("base_currency", "quote_currency", "source_last_update", "revision") in uniques
    assert ("base_currency", "quote_currency", "source_last_update") not in uniques
    if is_postgres():
        assert cols["rate"]["type"].precision is None and cols["rate"]["type"].scale is None


async def test_downgrade_refuses_while_corrections_are_stored(engine, migrated_url):
    ts = datetime(2026, 11, 23, 13, 21, 10, tzinfo=UTC)
    rows = [
        {"id": uuid4(), "base_currency": "USD", "quote_currency": "ILS", "rate": Decimal(r),
         "source_rate": Decimal(r), "source_unit": 1, "publication_date": date(2026, 11, 23),
         "source_last_update": ts, "retrieved_at": ts, "revision": rev}
        for rev, r in enumerate(["3.343", "3.341"])
    ]
    async with engine.begin() as conn:
        await conn.execute(rate_observations.insert(), rows)

    with pytest.raises(RuntimeError, match="Refusing to downgrade 0004"):
        await _run_sync_alembic(
            lambda: command.downgrade(alembic_config(migrated_url), "0003_refresh_status")
        )
    cols, _ = await _columns_and_uniques(migrated_url)
    assert "revision" in cols  # nothing was undone
