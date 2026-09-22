"""Cron scheduling arithmetic. Pure functions, no clock, no I/O — the scheduler loop
(``scheduler.py``) supplies "now".

Only classic 5-field cron is accepted. ``croniter`` also understands a 6th seconds field and
``@daily``-style macros; a v0 "basic cron-like schedule" has no need of either, and one-minute
granularity is a deliberate floor (a pipeline run is not a sub-minute unit of work).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter


def validate_schedule(cron: str, timezone: str) -> None:
    fields = cron.split()
    if len(fields) != 5:
        raise ValueError("cron must have exactly 5 fields: minute hour day-of-month month day-of-week")
    if not croniter.is_valid(cron):
        raise ValueError(f"{cron!r} is not a valid cron expression")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError) as e:
        raise ValueError(f"unknown timezone {timezone!r} (use an IANA name such as 'America/Toronto' or 'UTC')") from e


def next_fire(cron: str, timezone: str, after: datetime) -> datetime:
    """The first instant strictly after ``after`` that matches ``cron`` in ``timezone`` (UTC out).

    Evaluated in the schedule's own timezone so "09:00 daily" stays 09:00 across a DST change.
    """
    if after.tzinfo is None:
        raise ValueError("after must be timezone-aware")
    tz = ZoneInfo(timezone)
    local = after.astimezone(tz)
    nxt = croniter(cron, local).get_next(datetime)
    return nxt.astimezone(UTC)
