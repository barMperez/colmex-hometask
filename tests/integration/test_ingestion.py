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


# --- Re-stamps and publication dates (regression) ---------------------------
# BoI can move lastUpdate without publishing. Before the fix, a new lastUpdate was
# stored as a new publication dated by its calendar day, so a stale rate was
# ELIGIBLE after the day's deadline.

from rates.domain.models import BOI_TIMEZONE, EligibilityReason, EligibilityStatus  # noqa: E402
from rates.services.rate_query import RateQueryService  # noqa: E402


def il(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=BOI_TIMEZONE).astimezone(UTC)


def monday(**overrides) -> dict:
    """snapshot_2026-11-23.json with per-currency overrides: {"USD": {"lastUpdate": ...}}."""
    payload = json.loads(boi_fixture("snapshot_2026-11-23.json"), parse_float=Decimal)
    for entry in payload["exchangeRates"]:
        entry.update(overrides.get(entry["key"], {}))
    return payload


def restamped_all(at: datetime) -> dict:
    """Monday's snapshot, unchanged, with every lastUpdate moved to ``at``."""
    stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return monday(**{k: {"lastUpdate": stamp} for k in ("USD", "EUR", "GBP", "JPY", "LBP")})


class PayloadSource:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def fetch(self):
        return self._payload


async def ingest_payload(engine, payload: dict, at: datetime):
    return await IngestionService(engine, PayloadSource(payload), FixedClock(at)).run_once()


async def ingest_monday(engine):
    return await ingest_payload(engine, monday(), il(2026, 11, 23, 15, 25))


async def usd_at(engine, at: datetime):
    return await RateQueryService(engine, FixedClock(at)).get_rate("USD", "ILS")


def stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_restamp_inside_todays_window_is_not_a_publication(engine, caplog):
    """The reported gap: Monday's rate re-stamped Tue 17:00, Tuesday never published."""
    await ingest_monday(engine)

    with caplog.at_level("WARNING", logger="rates.services.ingestion"):
        result = await ingest_payload(engine, restamped_all(il(2026, 11, 24, 17)), il(2026, 11, 24, 17, 5))

    assert result.inserted == 0
    assert result.outcome == "success"  # BoI answered; nothing new is not a failure
    assert sorted(r.currency for r in result.restamped) == ["EUR", "GBP", "USD"]
    assert result.latest_publication_date == date(2026, 11, 23)
    assert await count(engine, rate_observations) == 3
    assert await count(engine, audit_events) == 1  # only Monday's ingestion
    assert caplog.text.count("rates.restamp_ignored") == 3

    view = await usd_at(engine, il(2026, 11, 24, 17, 10))
    assert view.observation.publication_date == date(2026, 11, 23)
    assert view.eligibility.status is EligibilityStatus.NOT_ELIGIBLE
    assert view.eligibility.reason is EligibilityReason.PUBLICATION_OVERDUE
    assert view.eligibility.required_publication_date == date(2026, 11, 24)


async def test_morning_restamp_stores_nothing_and_turns_overdue_at_the_deadline(engine):
    await ingest_monday(engine)
    await ingest_payload(engine, restamped_all(il(2026, 11, 24, 10)), il(2026, 11, 24, 10, 5))

    assert await count(engine, rate_observations) == 3
    assert (await usd_at(engine, il(2026, 11, 24, 10, 10))).eligibility.is_eligible
    late = await usd_at(engine, il(2026, 11, 24, 16, 1))
    assert late.eligibility.reason is EligibilityReason.PUBLICATION_OVERDUE


async def test_restamps_every_day_never_make_the_rate_eligible(engine):
    await ingest_monday(engine)
    for day in (24, 25, 26):  # BoI re-stamps Tue, Wed and Thu afternoons, never publishes
        await ingest_payload(engine, restamped_all(il(2026, 11, day, 17)), il(2026, 11, day, 17, 5))
        view = await usd_at(engine, il(2026, 11, day, 17, 10))
        assert view.eligibility.reason is EligibilityReason.PUBLICATION_OVERDUE
        assert view.observation.publication_date == date(2026, 11, 23)
    assert await count(engine, rate_observations) == 3


async def test_unchanged_rate_publication_is_eligible(engine):
    # BoI publishes Tuesday's USD rate equal to Monday's: currentChange is 0.
    await ingest_monday(engine)
    tuesday = monday(USD={"lastUpdate": stamp(il(2026, 11, 24, 15, 21)), "currentChange": 0})
    result = await ingest_payload(engine, tuesday, il(2026, 11, 24, 15, 25))

    assert result.inserted == 1
    assert result.restamped == []  # EUR/GBP kept Monday's lastUpdate: an idempotent re-fetch
    view = await usd_at(engine, il(2026, 11, 24, 16, 1))
    assert view.eligibility.is_eligible
    assert view.observation.publication_date == date(2026, 11, 24)
    assert view.observation.source_change == 0


async def test_restamp_then_real_publication_same_day(engine):
    await ingest_monday(engine)
    await ingest_payload(engine, restamped_all(il(2026, 11, 24, 10)), il(2026, 11, 24, 10, 5))
    tuesday = json.loads(boi_fixture("snapshot_2026-11-24.json"), parse_float=Decimal)
    result = await ingest_payload(engine, tuesday, il(2026, 11, 24, 15, 30))

    assert result.inserted == 3 and result.restamped == []
    view = await usd_at(engine, il(2026, 11, 24, 16, 1))
    assert view.eligibility.is_eligible
    assert view.observation.rate == Decimal("3.351")


async def test_restamp_of_todays_publication_keeps_it_current(engine):
    tuesday = json.loads(boi_fixture("snapshot_2026-11-24.json"), parse_float=Decimal)
    await ingest_payload(engine, tuesday, il(2026, 11, 24, 15, 30))
    for e in tuesday["exchangeRates"]:
        e["lastUpdate"] = stamp(il(2026, 11, 24, 18))
    result = await ingest_payload(engine, tuesday, il(2026, 11, 24, 18, 5))

    assert result.inserted == 0
    view = await usd_at(engine, il(2026, 11, 24, 18, 10))
    assert view.eligibility.is_eligible
    assert view.observation.source_last_update == datetime(2026, 11, 24, 13, 25, tzinfo=UTC)


async def test_morning_correction_is_stored_but_keeps_mondays_date(engine):
    await ingest_monday(engine)
    corrected = monday(USD={"lastUpdate": stamp(il(2026, 11, 24, 10)), "currentExchangeRate": Decimal("3.341"),
                            "currentChange": Decimal("0.45")})
    result = await ingest_payload(engine, corrected, il(2026, 11, 24, 10, 5))

    assert result.inserted == 1
    assert (await usd_at(engine, il(2026, 11, 24, 10, 10))).observation.rate == Decimal("3.341")
    late = await usd_at(engine, il(2026, 11, 24, 16, 1))
    assert late.observation.publication_date == date(2026, 11, 23)
    assert late.eligibility.reason is EligibilityReason.PUBLICATION_OVERDUE


async def test_mixed_snapshot_stores_publications_and_skips_restamps(engine):
    await ingest_monday(engine)
    tuesday = json.loads(boi_fixture("snapshot_2026-11-24.json"), parse_float=Decimal)
    usd = tuesday["exchangeRates"][0]
    assert usd["key"] == "USD"
    usd.update(currentExchangeRate=Decimal("3.343"), currentChange=monday()["exchangeRates"][0]["currentChange"],
               lastUpdate=stamp(il(2026, 11, 24, 17)))
    result = await ingest_payload(engine, tuesday, il(2026, 11, 24, 17, 5))

    assert result.inserted == 2
    assert [r.currency for r in result.restamped] == ["USD"]
    assert result.latest_publication_date == date(2026, 11, 24)
    async with engine.connect() as conn:
        audit = (await conn.execute(select(audit_events).order_by(audit_events.c.occurred_at.desc()))).mappings().first()
    assert sorted(o["pair"] for o in audit["after"]["observations"]) == ["EUR/ILS", "GBP/ILS"]
    assert (await usd_at(engine, il(2026, 11, 24, 17, 10))).eligibility.reason is EligibilityReason.PUBLICATION_OVERDUE


async def test_legacy_row_without_change_fails_closed(engine):
    """Rows stored before migration 0005 have no change: identical rate is a re-stamp."""
    await ingest_monday(engine)
    async with engine.begin() as conn:
        await conn.execute(rate_observations.update().values(source_change=None))
    result = await ingest_payload(engine, restamped_all(il(2026, 11, 24, 17)), il(2026, 11, 24, 17, 5))

    assert result.inserted == 0
    assert (await usd_at(engine, il(2026, 11, 24, 17, 10))).eligibility.reason is EligibilityReason.PUBLICATION_OVERDUE


async def test_source_change_round_trips_exactly(engine):
    changes = {"USD": Decimal("0.5111244738424534"), "EUR": Decimal("-0.0709026452140714"), "GBP": Decimal("0")}
    payload = monday(**{k: {"currentChange": v} for k, v in changes.items()})
    await ingest_payload(engine, payload, il(2026, 11, 23, 15, 25))
    async with engine.connect() as conn:
        rows = dict((await conn.execute(
            select(rate_observations.c.base_currency, rate_observations.c.source_change)
        )).all())
    assert rows == changes
    assert all(isinstance(v, Decimal) for v in rows.values())


async def test_missing_change_is_stored_as_null(engine):
    payload = monday()
    for e in payload["exchangeRates"]:
        e.pop("currentChange")
    await ingest_payload(engine, payload, il(2026, 11, 23, 15, 25))
    async with engine.connect() as conn:
        values = set((await conn.execute(select(rate_observations.c.source_change))).scalars())
    assert values == {None}


async def test_same_timestamp_change_only_difference_is_not_a_correction(engine):
    # usd_late_2026-11-24.json repeats Monday's USD lastUpdate with currentChange
    # rounded (0.51): the rate is unchanged, so no revision is created.
    await ingest_monday(engine)
    result = await IngestionService(
        engine, FakeSource("usd_late_2026-11-24.json"), FixedClock(il(2026, 11, 24, 15, 25))
    ).run_once()
    assert result.corrections == []
    async with engine.connect() as conn:
        usd_rows = (await conn.execute(
            select(func.count()).select_from(rate_observations).where(rate_observations.c.base_currency == "USD")
        )).scalar_one()
    assert usd_rows == 1


async def test_restamp_with_nothing_stored_is_a_publication_inside_the_window(engine):
    # Fresh database: there is nothing to compare with, so window timing decides.
    result = await ingest_payload(engine, restamped_all(il(2026, 11, 24, 17)), il(2026, 11, 24, 17, 5))
    assert result.inserted == 3 and result.restamped == []
    assert (await usd_at(engine, il(2026, 11, 24, 17, 10))).observation.publication_date == date(2026, 11, 24)


async def test_correction_outside_window_with_nothing_stored_is_rejected(engine):
    with pytest.raises(SnapshotRejected, match="no stored rate"):
        await ingest_payload(engine, restamped_all(il(2026, 11, 24, 10)), il(2026, 11, 24, 10, 5))
    assert await count(engine, rate_observations) == 0
