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
    assert "outcome=snapshot_rejected" in caplog.text
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
    assert sleeps == [300, 300]  # Monday 15:25 Israel: inside the publication window


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
