"""A repeating reminder moves to its next time: a day, a week, or the same day of
the next month, clamped at the month's end (Jan 31 -> Feb 28, then Mar 28)."""
from datetime import datetime, timezone

from routes.reminders import _next


def t(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_day_and_week():
    assert _next(t(2026, 10, 29, 9), "day") == t(2026, 10, 30, 9)
    assert _next(t(2026, 10, 29, 9), "week") == t(2026, 11, 5, 9)


def test_month_clamps_at_month_end_and_rolls_the_year():
    assert _next(t(2027, 1, 31, 9), "month") == t(2027, 2, 28, 9)
    assert _next(t(2026, 12, 15, 9), "month") == t(2027, 1, 15, 9)
