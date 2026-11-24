import asyncio
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from rates.adapters.boi.client import BoiFetchError
from rates.adapters.db.repos import SqlRefreshStatusRepository
from rates.adapters.db.tables import rate_observations
from rates.clock import FixedClock
from rates.domain.models import BOI_TIMEZONE
from rates.refresh.loop import RefreshLoop
from rates.services.ingestion import IngestionService
from rates.settings import Settings
from tests.fixtures import boi_fixture

NOW = datetime(2026, 11, 23, 15, 25, tzinfo=BOI_TIMEZONE).astimezone(UTC)
SETTINGS = Settings(_env_file=None)


class ScriptedSource:
    """Yields fixture payloads or raises exceptions, in order."""

    def __init__(self, *steps):
        self._steps = list(steps)
        self.calls = 0

    async def fetch(self):
        self.calls += 1
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return json.loads(boi_fixture(step), parse_float=Decimal)


async def snapshot_rows(engine):
    async with engine.connect() as conn:
        rows = (await conn.execute(select(rate_observations).order_by(rate_observations.c.base_currency))).mappings().all()
    return [dict(r) for r in rows]


def make_loop(engine, source, sleep=None):
    clock = FixedClock(NOW)
    kwargs = {"sleep": sleep} if sleep else {}
    return RefreshLoop(engine, IngestionService(engine, source, clock), clock, SETTINGS, **kwargs)


async def test_failed_fetch_preserves_data_and_loop_continues(engine, caplog):
    source = ScriptedSource(
        "snapshot_2026-11-23.json",
        BoiFetchError("timeout"),
        "missing_gbp.json",
        "same_day_correction.json",
    )
    loop = make_loop(engine, source)

    assert await loop.run_once() is True
    before = await snapshot_rows(engine)

    with caplog.at_level(logging.WARNING, logger="rates.refresh.loop"):
        assert await loop.run_once() is False
        assert await loop.run_once() is False
    assert "outcome=boi_unavailable" in caplog.text
    assert "outcome=partial" in caplog.text  # missing_gbp: USD/EUR duplicates, GBP rejected
    assert loop.consecutive_failures == 2
    assert await snapshot_rows(engine) == before  # nothing changed, timestamps included

    assert await loop.run_once() is True
    assert loop.consecutive_failures == 0
    assert loop.last_success_at == NOW


async def test_unexpected_error_does_not_kill_loop(engine):
    loop = make_loop(engine, ScriptedSource(ValueError("bug"), "snapshot_2026-11-23.json"))
    assert await loop.run_once() is False
    assert await loop.run_once() is True


async def test_run_forever_uses_window_interval_and_stops(engine):
    sleeps: list[float] = []
    stop = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            stop.set()

    source = ScriptedSource("snapshot_2026-11-23.json", "snapshot_2026-11-23.json")
    loop = make_loop(engine, source, sleep=fake_sleep)

    await asyncio.wait_for(loop.run_forever(stop), timeout=10)

    assert source.calls == 2
    assert sleeps == [60, 60]  # Monday 15:25 Israel: dense window (15:15 to 16:30)


# --- Durable refresh status ---------------------------------------------------

async def read_status(engine):
    async with engine.connect() as conn:
        return await SqlRefreshStatusRepository(conn).get()


async def test_status_records_success_failure_and_duplicate(engine):
    loop = make_loop(
        engine,
        ScriptedSource(
            "snapshot_2026-11-23.json", BoiFetchError("timeout"), "snapshot_2026-11-23.json"
        ),
    )

    assert await loop.run_once() is True
    status = await read_status(engine)
    assert status.last_success_at == NOW
    assert status.last_attempt_at == NOW
    assert status.last_attempt_outcome == "success"

    assert await loop.run_once() is False
    status = await read_status(engine)
    assert status.last_attempt_outcome == "boi_unavailable"
    assert status.consecutive_failures == 1
    assert status.last_success_at == NOW  # a failure never moves the last success

    assert await loop.run_once() is True  # duplicate snapshot: nothing new, still a success
    status = await read_status(engine)
    assert status.last_attempt_outcome == "success"
    assert status.consecutive_failures == 0


async def test_failing_status_write_keeps_loop_and_rates(engine, monkeypatch, caplog):
    class BrokenStatusRepo(SqlRefreshStatusRepository):
        async def _update(self, **values):
            raise RuntimeError("disk full")

    monkeypatch.setattr("rates.refresh.loop.SqlRefreshStatusRepository", BrokenStatusRepo)
    loop = make_loop(engine, ScriptedSource("snapshot_2026-11-23.json", BoiFetchError("x")))

    with caplog.at_level(logging.WARNING, logger="rates.refresh.loop"):
        assert await loop.run_once() is True
        assert await loop.run_once() is False
    assert caplog.text.count("refresh.status_write_failed") == 2
    assert len(await snapshot_rows(engine)) == 3  # ingested rates are untouched
    assert (await read_status(engine)).last_success_at is None


async def test_restore_status_on_startup(engine):
    earlier = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
    async with engine.begin() as conn:
        await SqlRefreshStatusRepository(conn).record_success(earlier)

    loop = make_loop(engine, ScriptedSource())
    await loop.restore_status()

    assert loop.last_success_at == earlier


async def test_partial_then_success(engine, caplog):
    loop = make_loop(engine, ScriptedSource("missing_gbp.json", "snapshot_2026-11-23.json"))

    with caplog.at_level(logging.WARNING, logger="rates.refresh.loop"):
        assert await loop.run_once() is False
    rejections = [r.getMessage() for r in caplog.records if "currency_rejected" in r.getMessage()]
    assert rejections == ["refresh.currency_rejected currency=GBP reason=missing"]
    assert all(r.levelno == logging.ERROR for r in caplog.records if "currency_rejected" in r.getMessage())
    status = await read_status(engine)
    assert status.last_attempt_outcome == "partial"
    assert status.consecutive_failures == 1
    assert status.last_success_at is None
    assert {r["base_currency"] for r in await snapshot_rows(engine)} == {"USD", "EUR"}

    assert await loop.run_once() is True
    status = await read_status(engine)
    assert status.last_attempt_outcome == "success"
    assert status.consecutive_failures == 0
    assert status.last_success_at == NOW


# --- Not-eligible transitions --------------------------------------------------

def il(*args) -> datetime:
    return datetime(*args, tzinfo=BOI_TIMEZONE).astimezone(UTC)


def pair_logs(caplog, pair: str) -> list[tuple[int, str]]:
    return [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.getMessage().startswith("rates.pair_") and f"pair={pair} " in r.getMessage()
    ]


async def test_not_eligible_transition_logged_once_and_recovery(engine, caplog):
    clock = FixedClock(il(2026, 11, 24, 10, 0))
    source = ScriptedSource(
        "snapshot_2026-11-23.json",     # 10:00: Monday's rates, all eligible
        "usd_late_2026-11-24.json",     # 16:05: EUR/GBP updated, USD still Monday's
        BoiFetchError("timeout"),       # 16:10: still overdue
        "snapshot_2026-11-24.json",     # 16:15: USD arrives
    )
    loop = RefreshLoop(engine, IngestionService(engine, source, clock), clock, SETTINGS)

    with caplog.at_level(logging.INFO, logger="rates.refresh.loop"):
        await loop.run_once()
        assert pair_logs(caplog, "USD/ILS") == []

        clock.set(il(2026, 11, 24, 16, 5))
        await loop.run_once()
        clock.set(il(2026, 11, 24, 16, 10))
        await loop.run_once()
        clock.set(il(2026, 11, 24, 16, 15))
        await loop.run_once()

    assert pair_logs(caplog, "USD/ILS") == [
        (
            logging.ERROR,
            "rates.pair_not_eligible pair=USD/ILS reason=PUBLICATION_OVERDUE "
            "publication_date=2026-11-23 required=2026-11-24",
        ),
        (logging.INFO, "rates.pair_eligible pair=USD/ILS publication_date=2026-11-24"),
    ]
    assert pair_logs(caplog, "EUR/ILS") == []
    assert pair_logs(caplog, "GBP/ILS") == []


async def test_not_eligible_at_first_assessment_is_logged(engine, caplog):
    clock = FixedClock(il(2026, 11, 24, 16, 30))  # e.g. a restart during an incident
    loop = RefreshLoop(
        engine, IngestionService(engine, ScriptedSource("snapshot_2026-11-23.json"), clock), clock, SETTINGS
    )
    with caplog.at_level(logging.ERROR, logger="rates.refresh.loop"):
        await loop.run_once()
    assert [lvl for lvl, _ in pair_logs(caplog, "USD/ILS")] == [logging.ERROR]


async def test_calendar_expiry_warning_once_per_day(engine, caplog):
    clock = FixedClock(il(2027, 11, 2, 9, 0))
    source = ScriptedSource(BoiFetchError("x"), BoiFetchError("x"), BoiFetchError("x"))
    loop = RefreshLoop(engine, IngestionService(engine, source, clock), clock, SETTINGS)

    with caplog.at_level(logging.WARNING, logger="rates.refresh.loop"):
        await loop.run_once()
        clock.set(il(2027, 11, 2, 17, 0))   # same Israel day
        await loop.run_once()
        clock.set(il(2027, 11, 3, 9, 0))    # next day
        await loop.run_once()

    warnings = [r.getMessage() for r in caplog.records if "calendar_expiring" in r.getMessage()]
    assert warnings == [
        "rates.calendar_expiring ends_on=2027-12-31 days_left=59",
        "rates.calendar_expiring ends_on=2027-12-31 days_left=58",
    ]


async def test_no_calendar_warning_far_from_the_end(engine, caplog):
    clock = FixedClock(il(2026, 11, 24, 9, 0))
    loop = RefreshLoop(
        engine, IngestionService(engine, ScriptedSource(BoiFetchError("x")), clock), clock, SETTINGS
    )
    with caplog.at_level(logging.WARNING, logger="rates.refresh.loop"):
        await loop.run_once()
    assert "calendar_expiring" not in caplog.text


class PayloadSource:
    def __init__(self, *payloads):
        self._payloads = list(payloads)

    async def fetch(self):
        return self._payloads.pop(0)


def restamped_monday(local: datetime) -> dict:
    payload = json.loads(boi_fixture("snapshot_2026-11-23.json"), parse_float=Decimal)
    for e in payload["exchangeRates"]:
        e["lastUpdate"] = local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return payload


async def test_restamp_only_poll_is_a_success_and_pair_goes_overdue(engine, caplog):
    """Regression: a re-stamp inside Tuesday's window used to make USD eligible again."""
    def il(d, hh, mm=0):
        return datetime(2026, 11, d, hh, mm, tzinfo=BOI_TIMEZONE).astimezone(UTC)

    clock = FixedClock(il(23, 15, 25))
    monday = json.loads(boi_fixture("snapshot_2026-11-23.json"), parse_float=Decimal)
    source = PayloadSource(monday, restamped_monday(il(24, 17)))
    loop = RefreshLoop(engine, IngestionService(engine, source, clock), clock, SETTINGS)

    with caplog.at_level(logging.INFO):
        assert await loop.run_once() is True
        clock.set(il(24, 17, 5))
        assert await loop.run_once() is True  # BoI answered; nothing new is not a failure

    assert loop.consecutive_failures == 0
    async with engine.connect() as conn:
        status = await SqlRefreshStatusRepository(conn).get()
    assert status.last_attempt_outcome == "success"
    assert len(await snapshot_rows(engine)) == 3
    assert "rates.restamp_ignored currency=USD" in caplog.text
    assert pair_logs(caplog, "USD/ILS") == [
        (
            logging.ERROR,
            "rates.pair_not_eligible pair=USD/ILS reason=PUBLICATION_OVERDUE "
            "publication_date=2026-11-23 required=2026-11-24",
        )
    ]
