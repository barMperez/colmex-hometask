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
