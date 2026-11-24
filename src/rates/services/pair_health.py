"""Per-pair eligibility assessment, shared by readiness (API) and the refresh loop.

Read-only: the caller supplies a connection. Evaluates every enabled pair's current
observation with the same pure ``evaluate()`` the read path uses.
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from rates.adapters.db.repos import (
    SqlCalendarRepository,
    SqlPairRepository,
    SqlPolicyRepository,
    SqlRateRepository,
)
from rates.domain.calendar import CalendarView
from rates.domain.eligibility import evaluate
from rates.domain.errors import PolicyNotEvaluable
from rates.domain.models import EligibilityStatus

REASON_RATE_NOT_FOUND = "RATE_NOT_FOUND"
REASON_POLICY_NOT_EVALUABLE = "POLICY_NOT_EVALUABLE"


@dataclass(frozen=True)
class PairAssessment:
    pair: str
    """``BASE/QUOTE``, e.g. ``USD/ILS``."""
    eligible: bool
    reason: str | None
    """An ``EligibilityReason`` value, ``RATE_NOT_FOUND`` or ``POLICY_NOT_EVALUABLE``."""
    publication_date: date | None
    required_publication_date: date | None


@dataclass(frozen=True)
class PairHealth:
    assessments: list[PairAssessment]
    calendar: CalendarView

    @property
    def not_eligible(self) -> list[PairAssessment]:
        return [a for a in self.assessments if not a.eligible]


async def assess_pairs(conn: AsyncConnection, now: datetime) -> PairHealth:
    pairs = await SqlPairRepository(conn).enabled_pairs()
    policy = await SqlPolicyRepository(conn).active()
    calendar = await SqlCalendarRepository(conn).load_view()
    rates = SqlRateRepository(conn)
    observations = {p: await rates.latest_for_pair(p.base, p.quote) for p in pairs}

    def name(p) -> str:
        return f"{p.base}/{p.quote}"

    assessments: list[PairAssessment] = []
    try:
        if policy is None:
            raise PolicyNotEvaluable("No active freshness policy")
        for pair, observation in observations.items():
            if observation is None:
                assessments.append(PairAssessment(name(pair), False, REASON_RATE_NOT_FOUND, None, None))
                continue
            result = evaluate(observation, now, calendar, policy)
            assessments.append(
                PairAssessment(
                    name(pair),
                    result.status is EligibilityStatus.ELIGIBLE,
                    result.reason.value if result.reason else None,
                    observation.publication_date,
                    result.required_publication_date,
                )
            )
    except PolicyNotEvaluable:
        assessments = [
            PairAssessment(
                name(pair),
                False,
                REASON_POLICY_NOT_EVALUABLE,
                observation.publication_date if observation else None,
                None,
            )
            for pair, observation in observations.items()
        ]
    return PairHealth(assessments, calendar)


@dataclass(frozen=True)
class CalendarExpiry:
    ends_on: date | None
    warning: bool


def calendar_expiry(calendar: CalendarView, today: date, warning_days: int) -> CalendarExpiry:
    """Coverage end of the loaded BoI calendar and whether it is close (or already gone)."""
    ends_on = calendar.coverage_end(today)
    return CalendarExpiry(ends_on, ends_on is None or (ends_on - today).days <= warning_days)
