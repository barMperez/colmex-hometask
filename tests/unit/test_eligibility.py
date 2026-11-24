"""Table-driven tests for specs/rate-freshness-policy (all non-operator scenarios)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from rates.domain.calendar import CalendarView, publication_deadline, required_publication_date
from rates.domain.eligibility import evaluate
from rates.domain.errors import PolicyNotEvaluable
from rates.domain.models import (
    BOI_TIMEZONE,
    EligibilityReason,
    EligibilityStatus,
    Policy,
    RateObservation,
)

POLICY = Policy(
    version=1,
    grace_minutes=45,
    max_age_days=7,
    weekday_publish_time=time(15, 15),
    friday_publish_time=time(12, 15),
    timezone="Asia/Jerusalem",
)

# Subset of the official BoI 2026 non-publication list relevant to these tests.
BOI_2026 = {
    date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 3), date(2026, 4, 6),
    date(2026, 4, 8), date(2026, 10, 27), date(2026, 12, 25),
}
CALENDAR = CalendarView(frozenset(BOI_2026), frozenset({2026, 2027}))


def il(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    """An Israel-local wall-clock time as a UTC instant."""
    return datetime(y, m, d, hh, mm, tzinfo=BOI_TIMEZONE).astimezone(UTC)


def observation(published: date) -> RateObservation:
    ts = datetime.combine(published, time(13, 21), tzinfo=UTC)
    return RateObservation(
        id=uuid4(),
        base_currency="USD",
        quote_currency="ILS",
        rate=Decimal("3.343"),
        source_rate=Decimal("3.343"),
        source_unit=1,
        publication_date=published,
        source_last_update=ts,
        retrieved_at=ts,
    )


# --- Business-day calendar -------------------------------------------------

@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 11, 28), False),  # Saturday
        (date(2026, 11, 29), False),  # Sunday
        (date(2026, 10, 27), False),  # listed: Knesset elections
        (date(2026, 11, 24), True),   # ordinary Tuesday
        (date(2026, 11, 27), True),   # ordinary Friday
    ],
)
def test_business_day(day, expected):
    assert CALENDAR.is_business_day(day) is expected


def test_business_day_uncovered_year_fails_closed():
    with pytest.raises(PolicyNotEvaluable):
        CALENDAR.is_business_day(date(2028, 1, 3))


# --- Publication deadline --------------------------------------------------

def test_weekday_deadline():
    assert publication_deadline(date(2026, 11, 24), POLICY) == il(2026, 11, 24, 16, 0)


def test_friday_deadline():
    assert publication_deadline(date(2026, 11, 27), POLICY) == il(2026, 11, 27, 13, 0)


def test_deadline_respects_dst():
    # Israel moves to summer time on Friday 2026-03-27.
    before = publication_deadline(date(2026, 3, 26), POLICY)  # Thu, UTC+2
    after = publication_deadline(date(2026, 3, 30), POLICY)   # Mon, UTC+3
    assert before == datetime(2026, 3, 26, 14, 0, tzinfo=UTC)
    assert after == datetime(2026, 3, 30, 13, 0, tzinfo=UTC)
    assert before.astimezone(BOI_TIMEZONE).time() == after.astimezone(BOI_TIMEZONE).time()


# --- Required publication date ---------------------------------------------

@pytest.mark.parametrize(
    ("now", "required"),
    [
        (il(2026, 11, 24, 10, 0), date(2026, 11, 23)),   # before today's deadline
        (il(2026, 11, 24, 15, 59), date(2026, 11, 23)),
        (il(2026, 11, 24, 16, 0), date(2026, 11, 24)),   # deadline is inclusive
        (il(2026, 11, 24, 16, 1), date(2026, 11, 24)),   # after today's deadline
        (il(2026, 11, 29, 9, 0), date(2026, 11, 27)),    # Sunday -> Friday
        (il(2026, 10, 27, 18, 0), date(2026, 10, 26)),   # listed holiday
        (il(2026, 4, 7, 10, 0), date(2026, 3, 31)),      # Pesach: 7 days back
    ],
)
def test_required_publication_date(now, required):
    assert required_publication_date(now, CALENDAR, POLICY) == required


# --- Eligibility evaluation ------------------------------------------------

@dataclass(frozen=True)
class Case:
    name: str
    now: datetime
    published: date
    status: EligibilityStatus
    reason: EligibilityReason | None = None
    required: date | None = None
    calendar: CalendarView = CALENDAR


OK = EligibilityStatus.ELIGIBLE
NOT = EligibilityStatus.NOT_ELIGIBLE

CASES = [
    Case("current rate is eligible", il(2026, 11, 24, 10, 0), date(2026, 11, 23), OK,
         required=date(2026, 11, 23)),
    Case("overdue rate is not eligible", il(2026, 11, 24, 16, 1), date(2026, 11, 23), NOT,
         EligibilityReason.PUBLICATION_OVERDUE, date(2026, 11, 24)),
    Case("today's rate after deadline", il(2026, 11, 24, 16, 1), date(2026, 11, 24), OK,
         required=date(2026, 11, 24)),
    Case("today's rate before deadline", il(2026, 11, 24, 15, 30), date(2026, 11, 24), OK,
         required=date(2026, 11, 23)),
    Case("friday rate over the weekend (Sat)", il(2026, 11, 28, 12, 0), date(2026, 11, 27), OK,
         required=date(2026, 11, 27)),
    Case("friday rate over the weekend (Sun)", il(2026, 11, 29, 9, 0), date(2026, 11, 27), OK,
         required=date(2026, 11, 27)),
    Case("monday morning still uses friday", il(2026, 11, 30, 10, 0), date(2026, 11, 27), OK,
         required=date(2026, 11, 27)),
    Case("friday deadline is 13:00", il(2026, 11, 27, 13, 1), date(2026, 11, 26), NOT,
         EligibilityReason.PUBLICATION_OVERDUE, date(2026, 11, 27)),
    Case("before friday deadline", il(2026, 11, 27, 12, 59), date(2026, 11, 26), OK,
         required=date(2026, 11, 26)),
    Case("listed holiday keeps previous rate", il(2026, 10, 27, 18, 0), date(2026, 10, 26), OK,
         required=date(2026, 10, 26)),
    Case("unexpected missed publication", il(2026, 12, 2, 16, 30), date(2026, 12, 1), NOT,
         EligibilityReason.PUBLICATION_OVERDUE, date(2026, 12, 2)),
    Case("pesach: 7-day-old rate still eligible", il(2026, 4, 7, 10, 0), date(2026, 3, 31), OK,
         required=date(2026, 3, 31)),
    Case("future-dated observation", il(2026, 11, 24, 10, 0), date(2026, 11, 25), NOT,
         EligibilityReason.INVALID_PUBLICATION_DATE, date(2026, 11, 23)),
    Case("max-age cap overrides wrong calendar", il(2026, 11, 26, 18, 0), date(2026, 11, 16), NOT,
         EligibilityReason.MAX_AGE_EXCEEDED, None,
         CalendarView(
             frozenset(date(2026, 11, 17) + timedelta(days=i) for i in range(10)),
             frozenset({2026}),
         )),
]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_evaluate(case: Case):
    result = evaluate(observation(case.published), case.now, case.calendar, POLICY)

    assert result.status is case.status
    assert result.reason == case.reason
    assert result.required_publication_date == case.required
    assert result.policy_version == 1
    assert result.evaluated_at == case.now
    assert result.evaluated_at.tzinfo is UTC


def test_calendar_year_not_loaded_fails_closed():
    with pytest.raises(PolicyNotEvaluable):
        evaluate(observation(date(2027, 12, 31)), il(2028, 1, 3, 18, 0), CALENDAR, POLICY)


def test_required_date_in_unloaded_year_fails_closed():
    only_2027 = CalendarView(frozenset({date(2027, 1, 1)}), frozenset({2027}))
    # Mon 2027-01-04 09:00: walk back passes Sun, Sat, Fri 01-01 (holiday), then 2026-12-31.
    with pytest.raises(PolicyNotEvaluable):
        evaluate(observation(date(2026, 12, 31)), il(2027, 1, 4, 9, 0), only_2027, POLICY)


def test_evaluate_rejects_naive_now():
    with pytest.raises(ValueError):
        evaluate(observation(date(2026, 11, 23)), datetime(2026, 11, 24, 10), CALENDAR, POLICY)


def test_observation_rejects_float_rate():
    with pytest.raises(TypeError):
        RateObservation(
            id=uuid4(), base_currency="USD", quote_currency="ILS",
            rate=3.343, source_rate=Decimal("3.343"), source_unit=1,  # type: ignore[arg-type]
            publication_date=date(2026, 11, 23),
            source_last_update=datetime(2026, 11, 23, tzinfo=UTC),
            retrieved_at=datetime(2026, 11, 23, tzinfo=UTC),
        )
