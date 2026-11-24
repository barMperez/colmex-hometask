"""Domain models. Rates are always ``Decimal``; instants are always timezone-aware."""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from functools import cached_property
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

BOI_TIMEZONE = ZoneInfo("Asia/Jerusalem")
"""BoI publication semantics (dates, deadlines) are defined in Israel local time."""


class EligibilityStatus(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


class EligibilityReason(StrEnum):
    PUBLICATION_OVERDUE = "PUBLICATION_OVERDUE"
    MAX_AGE_EXCEEDED = "MAX_AGE_EXCEEDED"
    INVALID_PUBLICATION_DATE = "INVALID_PUBLICATION_DATE"


@dataclass(frozen=True)
class CurrencyPair:
    base: str
    quote: str
    source_key: str


@dataclass(frozen=True)
class RateObservation:
    id: UUID
    base_currency: str
    quote_currency: str
    rate: Decimal
    """Normalised: 1 unit of base currency expressed in quote currency."""
    source_rate: Decimal
    source_unit: int
    publication_date: date
    source_last_update: datetime
    retrieved_at: datetime

    def __post_init__(self) -> None:
        _require_decimal("rate", self.rate)
        _require_decimal("source_rate", self.source_rate)
        if isinstance(self.source_unit, bool) or not isinstance(self.source_unit, int):
            raise TypeError("source_unit must be an int")
        _require_aware("source_last_update", self.source_last_update)
        _require_aware("retrieved_at", self.retrieved_at)


@dataclass(frozen=True)
class Policy:
    version: int
    grace_minutes: int
    max_age_days: int
    weekday_publish_time: time
    friday_publish_time: time
    timezone: str

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class Eligibility:
    status: EligibilityStatus
    reason: EligibilityReason | None
    required_publication_date: date | None
    evaluated_at: datetime
    policy_version: int

    @property
    def is_eligible(self) -> bool:
        return self.status is EligibilityStatus.ELIGIBLE


@dataclass(frozen=True)
class AuditEvent:
    id: UUID
    occurred_at: datetime
    actor: str
    event_type: str
    entity_type: str
    entity_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    reason: str | None


def _require_decimal(name: str, value: object) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class RefreshStatus:
    """What the refresh process last recorded; all fields empty before its first attempt."""

    last_attempt_at: datetime | None = None
    last_attempt_outcome: str | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
