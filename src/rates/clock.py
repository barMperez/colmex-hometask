"""Injectable clock. Nothing in the codebase calls ``datetime.now()`` directly."""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current instant as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Deterministic clock for tests."""

    def __init__(self, instant: datetime) -> None:
        self._instant = _require_aware(instant)

    def now(self) -> datetime:
        return self._instant

    def set(self, instant: datetime) -> None:
        self._instant = _require_aware(instant)

    def advance(self, delta: timedelta) -> None:
        self._instant = self._instant + delta


def _require_aware(instant: datetime) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Clock instants must be timezone-aware")
    return instant.astimezone(UTC)
