"""Read path: pair check -> policy + calendar -> latest observation -> evaluate.

Policy and calendar are re-read on every request (a few dozen rows), so an operator
declaration takes effect immediately with no cache to invalidate. BoI is never
contacted on this path.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine

from rates.adapters.db.repos import (
    SqlCalendarRepository,
    SqlPairRepository,
    SqlPolicyRepository,
    SqlRateRepository,
    SqlRefreshStatusRepository,
)
from rates.clock import Clock
from rates.domain.eligibility import evaluate
from rates.domain.errors import PolicyNotEvaluable, RateNotFound, StorageUnavailable, UnsupportedPair
from rates.domain.models import Eligibility, RateObservation

STORAGE_ERRORS = (OperationalError, InterfaceError, PoolTimeoutError, OSError)


@dataclass(frozen=True)
class RateView:
    observation: RateObservation
    eligibility: Eligibility


@dataclass(frozen=True)
class Readiness:
    latest_retrieved_at: datetime | None
    last_successful_ingestion_at: datetime | None


class RateQueryService:
    def __init__(self, engine: AsyncEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    async def get_rate(self, base: str, quote: str) -> RateView:
        try:
            async with self._engine.connect() as conn:
                if not await SqlPairRepository(conn).is_enabled(base, quote):
                    raise UnsupportedPair(base, quote)
                policy = await SqlPolicyRepository(conn).active()
                if policy is None:
                    raise PolicyNotEvaluable("No active freshness policy")
                calendar = await SqlCalendarRepository(conn).load_view()
                observation = await SqlRateRepository(conn).latest_for_pair(base, quote)
        except STORAGE_ERRORS as exc:
            raise StorageUnavailable("Rate storage is unavailable") from exc

        if observation is None:
            raise RateNotFound(base, quote)
        return RateView(observation, evaluate(observation, self._clock.now(), calendar, policy))

    async def readiness(self) -> Readiness:
        try:
            async with self._engine.connect() as conn:
                latest = await SqlRateRepository(conn).latest_retrieved_at()
                status = await SqlRefreshStatusRepository(conn).get()
            return Readiness(latest, status.last_success_at)
        except STORAGE_ERRORS as exc:
            raise StorageUnavailable("Rate storage is unavailable") from exc
