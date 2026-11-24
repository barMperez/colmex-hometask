from datetime import date

import pytest

from rates.domain.calendar import CalendarView


def view(*years: int) -> CalendarView:
    return CalendarView(frozenset(), frozenset(years))


@pytest.mark.parametrize(
    ("years", "today", "expected"),
    [
        ((2026, 2027), date(2026, 11, 24), date(2027, 12, 31)),
        ((2026, 2027), date(2027, 11, 2), date(2027, 12, 31)),
        ((2026, 2028), date(2026, 11, 24), date(2026, 12, 31)),  # gap ends coverage
        ((2026, 2027), date(2028, 1, 1), None),                   # current year not loaded
        ((2027,), date(2026, 11, 24), None),
    ],
)
def test_coverage_end(years, today, expected):
    assert view(*years).coverage_end(today) == expected


# --- Publication slots: which day's publication a BoI lastUpdate can be ---------

from datetime import UTC, datetime, time  # noqa: E402

from rates.domain.calendar import (  # noqa: E402
    expected_publication_time,
    latest_publication_slot,
    publication_deadline,
    publication_slot,
)
from rates.domain.errors import PolicyNotEvaluable  # noqa: E402
from rates.domain.models import BOI_TIMEZONE, Policy  # noqa: E402

POLICY = Policy(1, 45, 7, time(15, 15), time(12, 15), "Asia/Jerusalem")
HOLIDAY_TUE = CalendarView(frozenset({date(2026, 11, 24)}), frozenset({2026}))


def il(y, m, d, hh, mm=0, ss=0, us=0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, us, tzinfo=BOI_TIMEZONE).astimezone(UTC)


@pytest.mark.parametrize(
    ("instant", "slot", "latest"),
    [
        (il(2026, 11, 24, 15, 15), date(2026, 11, 24), date(2026, 11, 24)),          # at the time
        (il(2026, 11, 24, 15, 14, 59, 999999), None, date(2026, 11, 23)),            # 1 us early
        (il(2026, 11, 24, 23, 59), date(2026, 11, 24), date(2026, 11, 24)),          # evening
        (il(2026, 11, 24, 0, 30), None, date(2026, 11, 23)),                         # after midnight
        (il(2026, 11, 27, 12, 15), date(2026, 11, 27), date(2026, 11, 27)),          # Friday time
        (il(2026, 11, 27, 12, 14), None, date(2026, 11, 26)),
        (il(2026, 11, 28, 16, 0), None, date(2026, 11, 27)),                         # Saturday
        (il(2026, 11, 29, 16, 0), None, date(2026, 11, 27)),                         # Sunday
        (il(2026, 11, 30, 9, 0), None, date(2026, 11, 27)),                          # Monday morning
        (il(2026, 7, 1, 15, 15), date(2026, 7, 1), date(2026, 7, 1)),                # IDT (UTC+3)
        (il(2026, 7, 1, 15, 14), None, date(2026, 6, 30)),
    ],
)
def test_publication_slots(instant, slot, latest):
    cal = view(2026)
    assert publication_slot(instant, cal, POLICY) == slot
    assert latest_publication_slot(instant, cal, POLICY) == latest


def test_holiday_is_never_a_slot():
    assert publication_slot(il(2026, 11, 24, 16), HOLIDAY_TUE, POLICY) is None
    assert latest_publication_slot(il(2026, 11, 24, 16), HOLIDAY_TUE, POLICY) == date(2026, 11, 23)


def test_slot_uses_policy_times():
    late = Policy(2, 45, 7, time(16, 0), time(13, 0), "Asia/Jerusalem")
    assert publication_slot(il(2026, 11, 24, 15, 30), view(2026), late) is None
    assert publication_slot(il(2026, 11, 24, 16, 0), view(2026), late) == date(2026, 11, 24)


def test_latest_slot_is_bounded_by_max_age():
    every_day_off = CalendarView(
        frozenset(date(2026, 11, d) for d in range(10, 25)), frozenset({2026})
    )
    assert latest_publication_slot(il(2026, 11, 24, 16), every_day_off, POLICY) is None


def test_slots_need_a_loaded_calendar():
    with pytest.raises(PolicyNotEvaluable):
        publication_slot(il(2026, 11, 24, 16), view(2027), POLICY)
    with pytest.raises(PolicyNotEvaluable):
        latest_publication_slot(il(2027, 1, 1, 9), view(2027), POLICY)  # walks back into 2026


def test_deadline_is_expected_time_plus_grace():
    day = date(2026, 11, 24)
    assert expected_publication_time(day, POLICY) == il(2026, 11, 24, 15, 15)
    assert publication_deadline(day, POLICY) == il(2026, 11, 24, 16, 0)
    assert expected_publication_time(date(2026, 7, 1), POLICY) == datetime(2026, 7, 1, 12, 15, tzinfo=UTC)
