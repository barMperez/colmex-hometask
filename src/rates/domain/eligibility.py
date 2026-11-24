"""Eligibility evaluation: a pure function of observation, time, calendar and policy."""

from datetime import UTC, datetime

from rates.domain.calendar import CalendarView, required_publication_date
from rates.domain.errors import PolicyNotEvaluable
from rates.domain.models import (
    Eligibility,
    EligibilityReason,
    EligibilityStatus,
    Policy,
    RateObservation,
)


def evaluate(
    observation: RateObservation,
    now: datetime,
    calendar: CalendarView,
    policy: Policy,
) -> Eligibility:
    """Assess whether ``observation`` is acceptable for use at ``now``.

    Fails closed by raising ``PolicyNotEvaluable``:
    - before any other check, when the calendar does not cover the days the
      evaluation needs;
    - when no required publication date exists within the max age for an
      observation that is neither future-dated nor over age (implausible calendar).

    Check order: future-dated -> max-age cap -> unresolvable required date -> overdue.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)

    required = required_publication_date(now, calendar, policy)
    local_today = now.astimezone(policy.tz).date()
    published = observation.publication_date

    def result(reason: EligibilityReason | None) -> Eligibility:
        return Eligibility(
            status=EligibilityStatus.ELIGIBLE if reason is None else EligibilityStatus.NOT_ELIGIBLE,
            reason=reason,
            required_publication_date=required,
            evaluated_at=now,
            policy_version=policy.version,
        )

    if published > local_today:
        return result(EligibilityReason.INVALID_PUBLICATION_DATE)
    if (local_today - published).days > policy.max_age_days:
        return result(EligibilityReason.MAX_AGE_EXCEEDED)
    if required is None:
        raise PolicyNotEvaluable(
            f"No BoI business day within {policy.max_age_days} days of {local_today}; "
            "check the non-publication calendar"
        )
    if published < required:
        return result(EligibilityReason.PUBLICATION_OVERDUE)
    return result(None)
