"""BoI publication calendar: business days, deadlines and the required publication date."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from rates.domain.errors import PolicyNotEvaluable
from rates.domain.models import Policy

FRIDAY = 4


@dataclass(frozen=True)
class CalendarView:
    """In-memory snapshot of the non-publication calendar and the years it covers."""

    non_publication_days: frozenset[date]
    covered_years: frozenset[int]

    def is_business_day(self, day: date) -> bool:
        if day.year not in self.covered_years:
            raise PolicyNotEvaluable(
                f"No BoI non-publication calendar loaded for {day.year}"
            )
        return day.weekday() < 5 and day not in self.non_publication_days

    def coverage_end(self, today: date) -> date | None:
        """31 December of the last year in the unbroken run of loaded years from ``today``.

        None when ``today``'s own year is not loaded (the policy is already not
        evaluable). A gap ends coverage: 2026 + 2028 loaded means 2026-12-31.
        """
        if today.year not in self.covered_years:
            return None
        year = today.year
        while year + 1 in self.covered_years:
            year += 1
        return date(year, 12, 31)


def publication_deadline(day: date, policy: Policy) -> datetime:
    """Expected publication time for ``day`` plus grace, as a UTC instant.

    The local wall-clock time is fixed (15:15 Mon-Thu, 12:15 Fri by default), so the
    UTC instant shifts with Israeli daylight-saving time.
    """
    publish_time = (
        policy.friday_publish_time if day.weekday() == FRIDAY else policy.weekday_publish_time
    )
    local = datetime.combine(day, publish_time, tzinfo=policy.tz)
    return local.astimezone(UTC) + timedelta(minutes=policy.grace_minutes)


def required_publication_date(
    now: datetime, calendar: CalendarView, policy: Policy
) -> date | None:
    """Latest business day whose deadline is at or before ``now``.

    Walks back at most ``max_age_days + 1`` days. Returns ``None`` when no such day
    exists within that bound (anything older would fail the max-age cap anyway).
    Raises ``PolicyNotEvaluable`` if the walk needs a year with no loaded calendar.
    """
    local_today = now.astimezone(policy.tz).date()
    for offset in range(policy.max_age_days + 1):
        day = local_today - timedelta(days=offset)
        if calendar.is_business_day(day) and publication_deadline(day, policy) <= now:
            return day
    return None
