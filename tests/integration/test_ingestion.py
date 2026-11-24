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
    assert result.latest_publication_date == date(2026, 11, 23)
    assert result.latest_source_last_update == datetime(2026, 11, 23, 13, 21, 10, 710489, tzinfo=UTC)
    assert await count(engine, rate_observations) == 3
    async with engine.connect() as conn:
        audit = (await conn.execute(select(audit_events))).mappings().one()
        stored_ids = set((await conn.execute(select(rate_observations.c.id))).scalars())
    assert audit["event_type"] == "RATES_INGESTED"
    assert audit["actor"] == "refresh-process"
    assert audit["entity_type"] == "rate_ingestion"
    assert audit["entity_id"] == str(audit["id"])
    entries = audit["after"]["observations"]
    assert sorted(e["pair"] for e in entries) == ["EUR/ILS", "GBP/ILS", "USD/ILS"]
    assert {e["id"] for e in entries} == {str(i) for i in stored_ids}
    for e in entries:
        assert e["source_last_update"].startswith("2026-11-23T13:21:10.710489")
        assert e["publication_date"] == "2026-11-23"
    assert audit["after"]["rejected"] == []
    assert result.outcome == "success"


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


async def test_single_currency_correction_leaves_other_pairs_untouched(engine):
    await service(engine, "snapshot_2026-11-23.json").run_once()
    async with engine.connect() as conn:
        before = {
            p: await SqlRateRepository(conn).latest_for_pair(p, "ILS") for p in ("EUR", "GBP")
        }

    later = FixedClock(datetime(2026, 11, 23, 14, 10, tzinfo=UTC))
    result = await IngestionService(engine, FakeSource("usd_only_correction.json"), later).run_once()

    assert result.inserted == 1
    assert result.latest_source_last_update == datetime(2026, 11, 23, 14, 5, tzinfo=UTC)
    assert await count(engine, rate_observations) == 4
    async with engine.connect() as conn:
        repo = SqlRateRepository(conn)
        usd = await repo.latest_for_pair("USD", "ILS")
        after = {p: await repo.latest_for_pair(p, "ILS") for p in ("EUR", "GBP")}
        audits = (
            await conn.execute(select(audit_events).order_by(audit_events.c.occurred_at))
        ).mappings().all()
    assert usd.rate == Decimal("3.341")
    assert after == before
    assert len(audits) == 2
    assert [e["pair"] for e in audits[-1]["after"]["observations"]] == ["USD/ILS"]


async def test_missing_currency_stores_the_others_and_audits_rejection(engine):
    result = await service(engine, "missing_gbp.json").run_once()

    assert result.inserted == 2
    assert result.outcome == "partial"
    assert [(r.currency, r.reason) for r in result.rejected] == [("GBP", "missing")]
    async with engine.connect() as conn:
        stored = set((await conn.execute(select(rate_observations.c.base_currency))).scalars())
        audit = (await conn.execute(select(audit_events))).mappings().one()
    assert stored == {"USD", "EUR"}
    assert sorted(e["pair"] for e in audit["after"]["observations"]) == ["EUR/ILS", "USD/ILS"]
    assert audit["after"]["rejected"] == [{"currency": "GBP", "reason": "missing"}]


async def test_invalid_rate_stores_the_others(engine):
    result = await service(engine, "zero_rate.json").run_once()

    assert result.outcome == "partial"
    assert [r.currency for r in result.rejected] == ["EUR"]
    async with engine.connect() as conn:
        stored = set((await conn.execute(select(rate_observations.c.base_currency))).scalars())
    assert stored == {"USD", "GBP"}


@pytest.mark.parametrize("payload", ["future_last_update.json", "malformed"])
async def test_nothing_valid_stores_nothing(engine, payload):
    await service(engine, "snapshot_2026-11-23.json").run_once()

    class Malformed:
        async def fetch(self):
            return {"rates": []}

    source = Malformed() if payload == "malformed" else FakeSource(payload)
    with pytest.raises(SnapshotRejected):
        await IngestionService(engine, source, FixedClock(NOW)).run_once()

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


async def test_same_timestamp_correction_is_stored_as_revision(engine, caplog):
    await service(engine, "snapshot_2026-11-23.json").run_once()
    async with engine.connect() as conn:
        before = await SqlRateRepository(conn).latest_for_pair("USD", "ILS")

    corrected = json.loads(boi_fixture("snapshot_2026-11-23.json"), parse_float=Decimal)
    usd = next(e for e in corrected["exchangeRates"] if e["key"] == "USD")
    usd["currentExchangeRate"] = Decimal("3.341")  # same lastUpdate, restated value

    class Restated:
        async def fetch(self):
            return corrected

    with caplog.at_level("WARNING", logger="rates.services.ingestion"):
        later = FixedClock(datetime(2026, 11, 23, 14, 30, tzinfo=UTC))  # distinct audit time
        result = await IngestionService(engine, Restated(), later).run_once()

    assert result.inserted == 1
    assert [(c.pair, c.previous_rate, c.new_rate, c.revision) for c in result.corrections] == [
        ("USD/ILS", Decimal("3.343"), Decimal("3.341"), 1)
    ]
    async with engine.connect() as conn:
        after = await SqlRateRepository(conn).latest_for_pair("USD", "ILS")
        audit = (
            await conn.execute(select(audit_events).order_by(audit_events.c.occurred_at.desc()))
        ).mappings().first()
    assert after.rate == Decimal("3.341") and after.revision == 1 and after.id != before.id
    assert await count(engine, rate_observations) == 4  # 3 originals + 1 revision; nothing updated
    (entry,) = audit["after"]["corrections"]
    assert (entry["pair"], entry["previous_rate"], entry["new_rate"], entry["revision"]) == (
        "USD/ILS", "3.343", "3.341", 1
    )
    assert "rates.same_timestamp_correction pair=USD/ILS" in caplog.text
    assert "from=3.343 to=3.341" in caplog.text


async def test_identical_reingest_logs_no_correction(engine, caplog):
    svc = service(engine, "snapshot_2026-11-23.json", "snapshot_2026-11-23.json")
    await svc.run_once()
    with caplog.at_level("WARNING", logger="rates.services.ingestion"):
        again = await svc.run_once()
    assert again.inserted == 0 and again.corrections == []
    assert "same_timestamp_correction" not in caplog.text
    assert await count(engine, audit_events) == 1


async def test_morning_restatement_does_not_make_a_stale_rate_eligible(engine):
    """Regression: publicationDate was the calendar date of BoI's lastUpdate.

    Monday's rate is ingested; on Tuesday at 10:00 Israel time BoI re-stamps USD
    (new lastUpdate, same value) and never publishes Tuesday's rate. After Tuesday's
    16:00 deadline the rate must be PUBLICATION_OVERDUE, not ELIGIBLE.
    """
    from rates.domain.models import EligibilityReason, EligibilityStatus
    from rates.services.rate_query import RateQueryService

    await service(engine, "snapshot_2026-11-23.json").run_once()

    restated = json.loads(boi_fixture("snapshot_2026-11-23.json"), parse_float=Decimal)
    restated["exchangeRates"][0]["lastUpdate"] = "2026-11-24T08:00:00Z"  # USD, Tue 10:00 IL

    class Restated:
        async def fetch(self):
            return restated

    tuesday_morning = FixedClock(datetime(2026, 11, 24, 8, 5, tzinfo=UTC))
    result = await IngestionService(engine, Restated(), tuesday_morning).run_once()
    assert result.rejected == []
    assert result.latest_publication_date == date(2026, 11, 23)

    after_deadline = FixedClock(datetime(2026, 11, 24, 14, 1, tzinfo=UTC))  # Tue 16:01 IL
    view = await RateQueryService(engine, after_deadline).get_rate("USD", "ILS")

    assert view.observation.source_last_update == datetime(2026, 11, 24, 8, 0, tzinfo=UTC)
    assert view.observation.publication_date == date(2026, 11, 23)
    assert view.eligibility.status is EligibilityStatus.NOT_ELIGIBLE
    assert view.eligibility.reason is EligibilityReason.PUBLICATION_OVERDUE
    assert view.eligibility.required_publication_date == date(2026, 11, 24)
