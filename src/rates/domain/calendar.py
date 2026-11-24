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


def expected_publication_time(day: date, policy: Policy) -> datetime:
    """Expected publication time for ``day`` (no grace), as a UTC instant.

    The local wall-clock time is fixed (15:15 Mon-Thu, 12:15 Fri by default), so the
    UTC instant shifts with Israeli daylight-saving time.
    """
    publish_time = (
        policy.friday_publish_time if day.weekday() == FRIDAY else policy.weekday_publish_time
    )
    return datetime.combine(day, publish_time, tzinfo=policy.tz).astimezone(UTC)


def publication_deadline(day: date, policy: Policy) -> datetime:
    """Expected publication time for ``day`` plus grace, as a UTC instant."""
    return expected_publication_time(day, policy) + timedelta(minutes=policy.grace_minutes)


def publication_slot(last_update: datetime, calendar: CalendarView, policy: Policy) -> date | None:
    """The business day whose publication a BoI ``lastUpdate`` can be, or None.

    BoI publishes a day's representative rate soon after that day's expected time
    (15:15 Mon-Thu, 12:15 Fri), so a ``lastUpdate`` is day D's publication (or a
    same-day correction of it) only if it falls on business day D at or after D's
    expected time. Any other ``lastUpdate`` (before the expected time, at night, on a
    weekend or holiday) cannot be a new publication: it is a restatement of an
    earlier rate, and returning None tells the caller so.

    Raises ``PolicyNotEvaluable`` if the calendar does not cover the day.
    """
    day = last_update.astimezone(policy.tz).date()
    if calendar.is_business_day(day) and expected_publication_time(day, policy) <= last_update:
        return day
    return None


def latest_publication_slot(
    last_update: datetime, calendar: CalendarView, policy: Policy
) -> date | None:
    """Latest business day whose expected publication time is at or before ``last_update``.

    The most recent publication that a restated ``lastUpdate`` can belong to. Walks
    back at most ``max_age_days + 1`` days; None if there is no such day.
    """
    local_day = last_update.astimezone(policy.tz).date()
    for offset in range(policy.max_age_days + 1):
        day = local_day - timedelta(days=offset)
        if calendar.is_business_day(day) and expected_publication_time(day, policy) <= last_update:
            return day
    return None


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
