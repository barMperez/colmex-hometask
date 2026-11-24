"""Repository interfaces the services depend on (implemented in adapters/db/repos.py)."""

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol

from rates.domain.calendar import CalendarView
from rates.domain.models import AuditEvent, CurrencyPair, Policy, RateObservation


class PairRepository(Protocol):
    async def is_enabled(self, base: str, quote: str) -> bool: ...
    async def enabled_pairs(self) -> list[CurrencyPair]: ...


class RateRepository(Protocol):
    async def latest_for_pair(self, base: str, quote: str) -> RateObservation | None: ...
    async def insert_many(self, observations: Sequence[RateObservation]) -> list[RateObservation]: ...
    async def latest_retrieved_at(self) -> datetime | None: ...


class CalendarRepository(Protocol):
    async def load_view(self) -> CalendarView: ...
    async def exists(self, day: date) -> bool: ...
    async def add(
        self, day: date, reason: str, origin: str, created_by: str, created_at: datetime
    ) -> None: ...


class PolicyRepository(Protocol):
    async def active(self) -> Policy | None: ...


class AuditRepository(Protocol):
    async def record(self, event: AuditEvent) -> None: ...
