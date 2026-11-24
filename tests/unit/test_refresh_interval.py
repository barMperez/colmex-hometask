from datetime import UTC, date, datetime

import pytest

from rates.domain.calendar import CalendarView
from rates.domain.models import BOI_TIMEZONE
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
