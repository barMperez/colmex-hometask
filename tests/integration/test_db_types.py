"""DecimalText / UtcDateTime round trips on the configured dialect."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, select, text
from sqlalchemy.exc import StatementError

from rates.adapters.db.types import DecimalText, UtcDateTime, canonical_decimal
from tests.conftest import is_postgres

probe_metadata = MetaData()
probe = Table(
    "type_probe",
    probe_metadata,
    Column("id", Integer, primary_key=True),
    Column("amount", DecimalText),
    Column("at", UtcDateTime),
)


@pytest.fixture
async def probe_engine(engine):
    async with engine.begin() as conn:
        await conn.run_sync(probe_metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(probe_metadata.drop_all)


@pytest.mark.parametrize("value", ["3.343", "4.3785", "0.021026", "100", "0.00004", "0.0004"])
async def test_decimal_round_trips_exactly(probe_engine, value):
    async with probe_engine.begin() as conn:
        await conn.execute(probe.insert().values(id=1, amount=Decimal(value)))
        stored = (await conn.execute(select(probe.c.amount))).scalar_one()

    assert isinstance(stored, Decimal)
    assert stored == Decimal(value)
    assert format(stored, "f") == value


async def test_decimal_column_rejects_float(probe_engine):
    async with probe_engine.begin() as conn:
        with pytest.raises((StatementError, TypeError)):
            await conn.execute(probe.insert().values(id=1, amount=3.343))


async def test_sqlite_stores_decimal_as_text(probe_engine):
    if is_postgres():
        pytest.skip("SQLite-specific storage check")
    async with probe_engine.begin() as conn:
        await conn.execute(probe.insert().values(id=1, amount=Decimal("3.343")))
        raw = (await conn.execute(text("SELECT typeof(amount), amount FROM type_probe"))).one()
    assert tuple(raw) == ("text", "3.343")


async def test_utc_datetime_round_trip_normalises_to_utc(probe_engine):
    israel = timezone(timedelta(hours=2))
    original = datetime(2026, 11, 23, 15, 21, 10, 710489, tzinfo=israel)
    async with probe_engine.begin() as conn:
        await conn.execute(probe.insert().values(id=1, at=original))
        stored = (await conn.execute(select(probe.c.at))).scalar_one()

    assert stored == original
    assert stored.tzinfo is UTC
    assert stored.microsecond == 710489


async def test_utc_datetime_rejects_naive(probe_engine):
    async with probe_engine.begin() as conn:
        with pytest.raises((StatementError, ValueError)):
            await conn.execute(probe.insert().values(id=1, at=datetime(2026, 11, 23, 13, 0)))


def test_canonical_decimal():
    assert str(canonical_decimal(Decimal("3.3430000000"))) == "3.343"
    assert str(canonical_decimal(Decimal("100.0000"))) == "100"
    assert str(canonical_decimal(Decimal("0E-10"))) == "0"
