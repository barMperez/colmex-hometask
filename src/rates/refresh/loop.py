"""Refresh loop: the single writer of rate data.

Polls BoI frequently during the publication window on business days and rarely
otherwise. Never crashes on a failed attempt: it logs, keeps existing data intact,
and tries again on schedule.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from rates.adapters.boi.client import BoiFetchError
from rates.adapters.db.repos import (
    SqlCalendarRepository,
    SqlPolicyRepository,
    SqlRefreshStatusRepository,
)
from rates.clock import Clock
from rates.domain.calendar import FRIDAY, CalendarView, publication_deadline
from rates.domain.errors import PolicyNotEvaluable, SnapshotRejected
from rates.domain.models import BOI_TIMEZONE, Policy
from rates.services.ingestion import IngestionService
from rates.services.pair_health import assess_pairs, calendar_expiry
from rates.settings import Settings

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


def poll_interval_seconds(
    now: datetime,
    calendar: CalendarView | None,
    settings: Settings,
    policy: Policy | None = None,
) -> int:
    """Seconds until the next retrieval.

    - dense interval on business days from the policy's expected publication time
      until the publication deadline plus the dense tail (needs ``policy``);
    - window interval on business days inside the configured publication window;
    - off-window interval otherwise.
    """
    local = now.astimezone(BOI_TIMEZONE)
    try:
        business_day = calendar.is_business_day(local.date()) if calendar else local.weekday() < 5
    except PolicyNotEvaluable:
        business_day = local.weekday() < 5  # unknown calendar: poll as if it might publish
    if not business_day:
        return settings.refresh_off_window_interval_seconds
    if policy is not None and _in_dense_window(now, policy, settings):
        return settings.refresh_dense_interval_seconds
    if settings.refresh_window_start <= local.time() < settings.refresh_window_end:
        return settings.refresh_window_interval_seconds
    return settings.refresh_off_window_interval_seconds


def _in_dense_window(now: datetime, policy: Policy, settings: Settings) -> bool:
    day = now.astimezone(policy.tz).date()
    publish_time = (
        policy.friday_publish_time if day.weekday() == FRIDAY else policy.weekday_publish_time
    )
    start = datetime.combine(day, publish_time, tzinfo=policy.tz)
    end = publication_deadline(day, policy) + timedelta(minutes=settings.refresh_dense_tail_minutes)
    return start <= now <= end


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
        self._last_outcome = "success"
        self._pair_eligible: dict[str, bool] = {}
        """Last assessed eligibility per pair; absent means not yet assessed."""
        self._calendar_warned_on: date | None = None

    async def restore_status(self) -> None:
        """Load ``last_success_at`` from the database so logs stay continuous across restarts."""
        try:
            async with self._engine.connect() as conn:
                status = await SqlRefreshStatusRepository(conn).get()
        except (SQLAlchemyError, OSError) as exc:
            log.warning("refresh.status_read_failed error=%s", exc)
            return
        self.last_success_at = status.last_success_at

    async def run_once(self) -> bool:
        """One ingestion attempt, then a durable status write.

        Returns True on success. Never raises (except cancel). The status is written
        in its own transaction after ingestion has committed or rolled back, so a
        failed attempt is still recorded and a failed status write cannot undo rates.
        """
        ok = await self._attempt()
        await self._record_status(ok)
        await self._assess_pairs()
        return ok

    def _warn_calendar_expiry(self, calendar: CalendarView) -> None:
        """At most one warning per Israel calendar day while the calendar is close to its end."""
        today = self._clock.now().astimezone(BOI_TIMEZONE).date()
        expiry = calendar_expiry(calendar, today, self._settings.calendar_warning_days)
        if not expiry.warning or self._calendar_warned_on == today:
            return
        self._calendar_warned_on = today
        log.warning(
            "rates.calendar_expiring ends_on=%s days_left=%s",
            expiry.ends_on.isoformat() if expiry.ends_on else None,
            (expiry.ends_on - today).days if expiry.ends_on else None,
        )

    async def _assess_pairs(self) -> None:
        """Log once when a pair becomes not eligible, and once when it recovers."""
        try:
            async with self._engine.connect() as conn:
                health = await assess_pairs(conn, self._clock.now())
        except Exception as exc:  # noqa: BLE001 - monitoring must never stop the loop
            log.warning("rates.pair_assessment_failed error=%s", exc)
            return
        self._warn_calendar_expiry(health.calendar)
        for a in health.assessments:
            previous = self._pair_eligible.get(a.pair)
            self._pair_eligible[a.pair] = a.eligible
            if not a.eligible and previous is not False:
                log.error(
                    "rates.pair_not_eligible pair=%s reason=%s publication_date=%s required=%s",
                    a.pair,
                    a.reason,
                    a.publication_date.isoformat() if a.publication_date else None,
                    a.required_publication_date.isoformat() if a.required_publication_date else None,
                )
            elif a.eligible and previous is False:
                log.info(
                    "rates.pair_eligible pair=%s publication_date=%s",
                    a.pair,
                    a.publication_date.isoformat() if a.publication_date else None,
                )

    async def _record_status(self, ok: bool) -> None:
        try:
            async with self._engine.begin() as conn:
                repo = SqlRefreshStatusRepository(conn)
                if ok:
                    written = await repo.record_success(self.last_success_at)
                else:
                    written = await repo.record_failure(
                        self._clock.now(), self._last_outcome, self.consecutive_failures
                    )
        except Exception as exc:  # noqa: BLE001 - status is best effort; logs remain the fallback
            log.warning("refresh.status_write_failed error=%s", exc)
            return
        if not written:
            log.warning("refresh.status_write_failed error=refresh_status row missing")

    async def _attempt(self) -> bool:
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

        if result.rejected:
            # Valid currencies were stored, but the attempt is not a success: a currency
            # silently missing must not look healthy.
            for rejected in result.rejected:
                log.error(
                    "refresh.currency_rejected currency=%s reason=%s",
                    rejected.currency,
                    rejected.reason,
                )
            self.consecutive_failures += 1
            self._last_outcome = "partial"
            log.warning(
                "refresh.attempt outcome=partial inserted=%d rejected=%s consecutive_failures=%d "
                "last_success_at=%s",
                result.inserted,
                ",".join(r.currency for r in result.rejected),
                self.consecutive_failures,
                self.last_success_at.isoformat() if self.last_success_at else None,
            )
            return False

        self.last_success_at = self._clock.now()
        self.consecutive_failures = 0
        log.info(
            "refresh.attempt outcome=success inserted=%d latest_publication_date=%s "
            "latest_source_last_update=%s duration_ms=%d last_success_at=%s",
            result.inserted,
            result.latest_publication_date.isoformat(),
            result.latest_source_last_update.isoformat(),
            (self.last_success_at - started).total_seconds() * 1000,
            self.last_success_at.isoformat(),
        )
        return True

    def _failed(self, outcome: str, exc: Exception, already_logged: bool = False) -> bool:
        self.consecutive_failures += 1
        self._last_outcome = outcome
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
        calendar: CalendarView | None = None
        policy: Policy | None = None
        try:
            async with self._engine.connect() as conn:
                calendar = await SqlCalendarRepository(conn).load_view()
                policy = await SqlPolicyRepository(conn).active()
        except (SQLAlchemyError, OSError):
            pass  # fall back to the plain window / off-window intervals
        return poll_interval_seconds(self._clock.now(), calendar, self._settings, policy)

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        await self.restore_status()
        log.info(
            "refresh.started url=%s last_success_at=%s",
            self._settings.boi_url,
            self.last_success_at.isoformat() if self.last_success_at else None,
        )
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
