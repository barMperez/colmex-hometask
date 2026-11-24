"""Refresh loop: the single writer of rate data.

Polls BoI frequently during the publication window on business days and rarely
otherwise. Never crashes on a failed attempt: it logs, keeps existing data intact,
and tries again on schedule.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from rates.adapters.boi.client import BoiFetchError
from rates.adapters.db.repos import SqlCalendarRepository
from rates.clock import Clock
from rates.domain.calendar import CalendarView
from rates.domain.errors import PolicyNotEvaluable, SnapshotRejected
from rates.domain.models import BOI_TIMEZONE
from rates.services.ingestion import IngestionService
from rates.settings import Settings

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


def poll_interval_seconds(now: datetime, calendar: CalendarView | None, settings: Settings) -> int:
    local = now.astimezone(BOI_TIMEZONE)
    try:
        business_day = calendar.is_business_day(local.date()) if calendar else local.weekday() < 5
    except PolicyNotEvaluable:
        business_day = local.weekday() < 5  # unknown calendar: poll as if it might publish
    in_window = settings.refresh_window_start <= local.time() < settings.refresh_window_end
    if business_day and in_window:
        return settings.refresh_window_interval_seconds
    return settings.refresh_off_window_interval_seconds


class RefreshLoop:
    def __init__(
        self,
        engine: AsyncEngine,
        ingestion: IngestionService,
        clock: Clock,
        settings: Settings,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._engine = engine
        self._ingestion = ingestion
        self._clock = clock
        self._settings = settings
        self._sleep = sleep
        self.last_success_at: datetime | None = None
        self.consecutive_failures = 0

    async def run_once(self) -> bool:
        """One ingestion attempt. Returns True on success. Never raises (except cancel)."""
        started = self._clock.now()
        try:
            result = await self._ingestion.run_once()
        except SnapshotRejected as exc:
            return self._failed("snapshot_rejected", exc)
        except BoiFetchError as exc:
            return self._failed("boi_unavailable", exc)
        except (SQLAlchemyError, OSError) as exc:
            return self._failed("storage_error", exc)
        except Exception as exc:  # noqa: BLE001 - the loop must survive unexpected bugs
            log.exception("refresh.attempt outcome=unexpected_error")
            return self._failed("unexpected_error", exc, already_logged=True)

        self.last_success_at = self._clock.now()
        self.consecutive_failures = 0
        log.info(
            "refresh.attempt outcome=success inserted=%d publication_date=%s source_last_update=%s "
            "duration_ms=%d last_success_at=%s",
            result.inserted,
            result.publication_date.isoformat(),
            result.source_last_update.isoformat(),
            (self.last_success_at - started).total_seconds() * 1000,
            self.last_success_at.isoformat(),
        )
        return True

    def _failed(self, outcome: str, exc: Exception, already_logged: bool = False) -> bool:
        self.consecutive_failures += 1
        if not already_logged:
            log.warning(
                "refresh.attempt outcome=%s consecutive_failures=%d last_success_at=%s error=%s",
                outcome,
                self.consecutive_failures,
                self.last_success_at.isoformat() if self.last_success_at else None,
                exc,
            )
        return False

    async def next_interval(self) -> int:
        calendar: CalendarView | None
        try:
            async with self._engine.connect() as conn:
                calendar = await SqlCalendarRepository(conn).load_view()
        except (SQLAlchemyError, OSError):
            calendar = None
        return poll_interval_seconds(self._clock.now(), calendar, self._settings)

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        log.info("refresh.started url=%s", self._settings.boi_url)
        while not stop.is_set():
            started = self._clock.now()
            await self.run_once()
            interval = await self.next_interval()
            elapsed = (self._clock.now() - started).total_seconds()
            delay = max(0.0, interval - elapsed)
            log.debug("refresh.sleep seconds=%.1f", delay)
            sleep_task = asyncio.ensure_future(self._sleep(delay))
            stop_task = asyncio.ensure_future(stop.wait())
            done, pending = await asyncio.wait(
                {sleep_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
        log.info("refresh.stopped")
