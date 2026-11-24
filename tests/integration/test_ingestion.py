import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from rates.adapters.db.repos import SqlAuditRepository, SqlRateRepository
from rates.adapters.db.tables import audit_events, rate_observations
from rates.clock import FixedClock
from rates.domain.errors import SnapshotRejected
from rates.services.ingestion import IngestionService
from tests.fixtures import boi_fixture

NOW = datetime(2026, 11, 23, 13, 25, tzinfo=UTC)


class FakeSource:
    def __init__(self, *fixtures: str) -> None:
        self._fixtures = list(fixtures)

    async def fetch(self):
        return json.loads(boi_fixture(self._fixtures.pop(0)), parse_float=Decimal)


def service(engine, *fixtures: str) -> IngestionService:
    return IngestionService(engine, FakeSource(*fixtures), FixedClock(NOW))


async def count(engine, table) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(select(func.count()).select_from(table))).scalar_one()


async def test_ingest_snapshot(engine):
    result = await service(engine, "snapshot_2026-11-23.json").run_once()

    assert result.inserted == 3
    assert result.publication_date == date(2026, 11, 23)
    assert await count(engine, rate_observations) == 3
    async with engine.connect() as conn:
        audit = (await conn.execute(select(audit_events))).mappings().one()
    assert audit["event_type"] == "RATES_INGESTED"
    assert audit["actor"] == "refresh-process"
    assert sorted(audit["after"]["pairs"]) == ["EUR/ILS", "GBP/ILS", "USD/ILS"]
    assert audit["after"]["source_last_update"].startswith("2026-11-23T13:21:10.710489")


async def test_reingest_is_idempotent_and_not_audited(engine):
    svc = service(engine, "snapshot_2026-11-23.json", "snapshot_2026-11-23.json")
    await svc.run_once()
    again = await svc.run_once()

    assert again.inserted == 0
    assert await count(engine, rate_observations) == 3
    assert await count(engine, audit_events) == 1


async def test_same_day_correction_kept_and_becomes_current(engine):
    await service(engine, "snapshot_2026-11-23.json").run_once()
    later = FixedClock(datetime(2026, 11, 23, 14, 10, tzinfo=UTC))
    await IngestionService(engine, FakeSource("same_day_correction.json"), later).run_once()

    assert await count(engine, rate_observations) == 6
    async with engine.connect() as conn:
        current = await SqlRateRepository(conn).latest_for_pair("USD", "ILS")
    assert current.rate == Decimal("3.341")


@pytest.mark.parametrize("fixture", ["missing_gbp.json", "zero_rate.json", "future_last_update.json"])
async def test_rejected_snapshot_stores_nothing(engine, fixture):
    await service(engine, "snapshot_2026-11-23.json").run_once()

    with pytest.raises(SnapshotRejected):
        await service(engine, fixture).run_once()

    assert await count(engine, rate_observations) == 3
    assert await count(engine, audit_events) == 1


async def test_failure_mid_write_rolls_back_whole_snapshot(engine, monkeypatch):
    async def boom(self, event):
        raise RuntimeError("storage failed after inserts")

    monkeypatch.setattr(SqlAuditRepository, "record", boom)

    with pytest.raises(RuntimeError):
        await service(engine, "snapshot_2026-11-23.json").run_once()

    assert await count(engine, rate_observations) == 0


async def test_decimal_round_trip_from_boi_to_storage(engine):
    """Task 5.6: fixture 3.343 comes back exactly as Decimal('3.343')."""
    await service(engine, "snapshot_2026-11-23.json").run_once()
    async with engine.connect() as conn:
        usd = await SqlRateRepository(conn).latest_for_pair("USD", "ILS")
    assert usd.rate == Decimal("3.343")
    assert str(usd.rate) == "3.343"
    assert isinstance(usd.source_rate, Decimal)
