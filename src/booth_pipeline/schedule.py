"""Cron scheduling arithmetic. Pure functions, no clock, no I/O — the scheduler loop
(``scheduler.py``) supplies "now".

Only classic 5-field cron is accepted. ``croniter`` also understands a 6th seconds field and
``@daily``-style macros; a v0 "basic cron-like schedule" has no need of either, and one-minute
granularity is a deliberate floor (a pipeline run is not a sub-minute unit of work).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

if TYPE_CHECKING:
    from .model import CronTrigger, IntervalTrigger


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


def next_fire_trigger(trigger: CronTrigger | IntervalTrigger, after: datetime) -> datetime:
    """``next_fire`` generalised over the ``Trigger`` union (ADR 0065): a ``CronTrigger`` behaves
    exactly as before; an ``IntervalTrigger`` simply fires ``seconds`` after ``after`` — no
    calendar alignment, since an interval has none to keep."""
    from .model import CronTrigger

    if isinstance(trigger, CronTrigger):
        return next_fire(trigger.cron, trigger.timezone, after)
    if after.tzinfo is None:
        raise ValueError("after must be timezone-aware")
    return after + timedelta(seconds=trigger.seconds)


def latest_slot(trigger: CronTrigger | IntervalTrigger, first_due: datetime, now: datetime) -> datetime:
    """The most recent slot due by ``now``, when ``first_due`` (a claimed ``next_run_at``) is the oldest
    slot not yet run. Missed slots coalesce into one run (ADR 0065), and that run is for this slot: the
    latest one, not the oldest. Normally there is one due slot and this is ``first_due`` itself."""
    from .model import CronTrigger

    if first_due >= now:
        return first_due
    if isinstance(trigger, CronTrigger):
        tz = ZoneInfo(trigger.timezone)
        # get_prev is strictly before its start; cron has one-minute granularity, so +1 s includes `now`.
        prev = croniter(trigger.cron, (now + timedelta(seconds=1)).astimezone(tz)).get_prev(datetime).astimezone(UTC)
        return max(first_due, prev)
    missed = int((now - first_due).total_seconds() // trigger.seconds)
    return first_due + timedelta(seconds=missed * trigger.seconds)
