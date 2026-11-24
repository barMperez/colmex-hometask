from datetime import UTC, date, datetime, time

import pytest

from rates.domain.calendar import CalendarView
from rates.domain.models import BOI_TIMEZONE, Policy
from rates.refresh.loop import poll_interval_seconds
from rates.settings import Settings

SETTINGS = Settings(_env_file=None, refresh_window_interval_seconds=300, refresh_off_window_interval_seconds=3600)
CALENDAR = CalendarView(frozenset({date(2026, 10, 27)}), frozenset({2026}))


def il(*args) -> datetime:
    return datetime(*args, tzinfo=BOI_TIMEZONE).astimezone(UTC)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (il(2026, 11, 24, 15, 20), 300),   # business day, in window
        (il(2026, 11, 24, 12, 0), 300),    # window start inclusive
        (il(2026, 11, 24, 18, 0), 3600),   # window end exclusive
        (il(2026, 11, 24, 9, 0), 3600),    # business day, before window
        (il(2026, 11, 28, 15, 20), 3600),  # Saturday
        (il(2026, 10, 27, 15, 20), 3600),  # listed holiday
    ],
)
def test_poll_interval(now, expected):
    assert poll_interval_seconds(now, CALENDAR, SETTINGS) == expected


def test_unknown_calendar_polls_weekdays_in_window():
    assert poll_interval_seconds(il(2028, 1, 4, 15, 20), CALENDAR, SETTINGS) == 300
    assert poll_interval_seconds(il(2026, 11, 24, 15, 20), None, SETTINGS) == 300


# --- Dense polling around the publication deadline ---------------------------

POLICY = Policy(
    version=1,
    grace_minutes=45,
    max_age_days=7,
    weekday_publish_time=time(15, 15),
    friday_publish_time=time(12, 15),
    timezone="Asia/Jerusalem",
)
DENSE = Settings(
    _env_file=None,
    refresh_window_interval_seconds=300,
    refresh_off_window_interval_seconds=3600,
    refresh_dense_interval_seconds=60,
    refresh_dense_tail_minutes=30,
)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (il(2026, 11, 24, 15, 14), 300),   # Tue, just before publish time
        (il(2026, 11, 24, 15, 15), 60),    # Tue, publish time (inclusive)
        (il(2026, 11, 24, 15, 20), 60),
        (il(2026, 11, 24, 16, 29), 60),
        (il(2026, 11, 24, 16, 30), 60),    # deadline 16:00 + 30 min tail (inclusive)
        (il(2026, 11, 24, 16, 31), 300),   # back to the window interval
        (il(2026, 11, 24, 14, 0), 300),
        (il(2026, 11, 27, 12, 30), 60),    # Friday: 12:15 publish, 13:00 deadline
        (il(2026, 11, 27, 13, 31), 300),
        (il(2026, 11, 28, 15, 20), 3600),  # Saturday
        (il(2026, 10, 27, 15, 20), 3600),  # listed holiday: not dense
    ],
)
def test_dense_interval_around_deadline(now, expected):
    assert poll_interval_seconds(now, CALENDAR, DENSE, POLICY) == expected


def test_no_policy_falls_back_to_window_interval():
    assert poll_interval_seconds(il(2026, 11, 24, 15, 20), CALENDAR, DENSE, None) == 300
