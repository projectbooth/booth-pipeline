from __future__ import annotations

from datetime import UTC, datetime

import pytest

from booth_pipeline.schedule import next_fire


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


def test_next_fire_is_strictly_after():
    assert next_fire("*/15 * * * *", "UTC", utc(2026, 1, 1, 10, 0)) == utc(2026, 1, 1, 10, 15)
    assert next_fire("*/15 * * * *", "UTC", utc(2026, 1, 1, 10, 7, 30)) == utc(2026, 1, 1, 10, 15)


def test_schedule_is_evaluated_in_its_own_timezone_across_dst():
    # 09:00 America/Toronto: UTC-5 in January (14:00Z), UTC-4 in July (13:00Z). The wall-clock
    # time must hold across the DST change, which is the whole point of a timezone field.
    assert next_fire("0 9 * * *", "America/Toronto", utc(2026, 1, 15, 0, 0)) == utc(2026, 1, 15, 14, 0)
    assert next_fire("0 9 * * *", "America/Toronto", utc(2026, 7, 15, 0, 0)) == utc(2026, 7, 15, 13, 0)


def test_naive_datetimes_are_refused():
    with pytest.raises(ValueError):
        next_fire("* * * * *", "UTC", datetime(2026, 1, 1))
