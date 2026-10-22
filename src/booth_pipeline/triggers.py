"""Event triggers (ADR 0123): reading an event off the bus, matching it, and the debounce rules.

Pure functions over ``EventTriggerState``. The store applies them inside a transaction that holds
the state row's lock (``ingest_trigger_event``, ``claim_due_event_triggers``, ``finish_run``), so
the rules live here once and the memory and PostgreSQL stores can't drift apart.

The life of a burst:

* each matching event pushes ``fire_at`` to ``debounce`` seconds after it, but never past
  ``max_wait`` seconds after the burst's first event, nor sooner than ``min_gap`` after the last fire;
* when ``fire_at`` comes, the scheduler claims it (``on_due``): with no active run the run starts and
  takes the burst's events with it; with one, ``queue_one`` remembers a follow-up and ``skip`` drops it;
* when a run ends, a remembered follow-up is due at once (``on_run_finished``).

Delivery is at least once. A redelivered message is recognised by its stream sequence before any of
this runs; anything else that arrives twice only extends a pending burst, it can't add a run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .model import EventTrigger
from .records import EventTriggerState

STORAGE_CREATED = "storage.object.created"
API_COMMITTED = "api.write.committed"
EVENT_TYPES = (STORAGE_CREATED, API_COMMITTED)
SUBJECTS = tuple(f"booth.*.{t}" for t in EVENT_TYPES)

# How many of a burst's events a run is told about (ctx.trigger.events); the count is always exact.
MAX_RUN_EVENTS = 100


class BadEvent(ValueError):
    """Not an event this module can act on (malformed, or its subject and envelope disagree)."""


@dataclass(frozen=True)
class TriggerEvent:
    workspace: str
    type: str  # STORAGE_CREATED or API_COMMITTED
    published_at: str
    principal: str
    backend_id: str = ""
    path: str = ""
    size: int | None = None
    via: str = ""
    api_slug: str = ""
    dataset: str = ""
    table: str = ""
    operation: str = ""
    rows: int | None = None

    def record(self) -> dict[str, Any]:
        """What a run (``ctx.trigger.events``) and the trigger's "last event" line see."""
        if self.type == STORAGE_CREATED:
            return {"type": self.type, "publishedAt": self.published_at, "principal": self.principal,
                    "backendId": self.backend_id, "path": self.path, "size": self.size, "via": self.via}  # fmt: skip
        return {"type": self.type, "publishedAt": self.published_at, "principal": self.principal,
                "apiSlug": self.api_slug, "dataset": self.dataset, "table": self.table,
                "operation": self.operation, "rows": self.rows}  # fmt: skip


def _str(data: dict, key: str, required: bool = True) -> str:
    v = data.get(key)
    if v is None and not required:
        return ""
    if not isinstance(v, str) or (required and not v):
        raise BadEvent(f"data.{key} is missing or not a string")
    return v


def _int(data: dict, key: str) -> int | None:
    v = data.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise BadEvent(f"data.{key} is not an integer")
    return v


def parse_event(subject: str, body: bytes) -> TriggerEvent:
    """An event off ``booth.<workspace>.<type>``. As booth-catalog does (ADR 0046 rule 4), the
    subject is authoritative: an envelope naming another workspace or type is refused."""
    parts = subject.split(".", 2)
    if len(parts) != 3 or parts[0] != "booth" or parts[2] not in EVENT_TYPES:
        raise BadEvent(f"not a subject this module subscribes to: {subject!r}")
    workspace, etype = parts[1], parts[2]
    try:
        env = json.loads(body)
    except ValueError as e:
        raise BadEvent(f"not JSON: {e}") from None
    if not isinstance(env, dict) or not isinstance(env.get("data"), dict):
        raise BadEvent("not an event envelope")
    if env.get("workspace") != workspace or env.get("eventType") != etype:
        raise BadEvent(f"the envelope says {env.get('workspace')!r}/{env.get('eventType')!r} but the subject says {workspace!r}/{etype!r}")
    data = env["data"]
    published_at = env.get("publishedAt") if isinstance(env.get("publishedAt"), str) else ""
    if etype == STORAGE_CREATED:
        path = _str(data, "path").lstrip("/")
        return TriggerEvent(workspace, etype, published_at, _str(data, "principal"), backend_id=_str(data, "backendId"),
                            path=path, size=_int(data, "size"), via=_str(data, "via", required=False))  # fmt: skip
    return TriggerEvent(workspace, etype, published_at, _str(data, "principal"), api_slug=_str(data, "apiSlug"),
                        dataset=_str(data, "dataset", required=False), table=_str(data, "table", required=False),
                        operation=_str(data, "operation", required=False), rows=_int(data, "rows"))  # fmt: skip


def matches(trigger: EventTrigger, ev: TriggerEvent) -> bool:
    for m in trigger.match:
        if m.storage is not None and ev.type == STORAGE_CREATED:
            if m.storage.backend_id == ev.backend_id and ev.path.startswith(m.storage.prefix):
                return True
        if m.api is not None and ev.type == API_COMMITTED:
            if ev.api_slug in m.api.slugs or (m.api.slug_prefix and ev.api_slug.startswith(m.api.slug_prefix)):
                return True
    return False


def concerns(trigger: EventTrigger, since: datetime | None, ev: TriggerEvent) -> bool:
    """The event matches, and was published after the trigger was turned on or its match last changed:
    a trigger must not fire for a burst it was never watching (a consumer catching up, a replay)."""
    if not matches(trigger, ev):
        return False
    if since is None or not ev.published_at:
        return True
    try:
        published = datetime.fromisoformat(ev.published_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if published.tzinfo is None:
        return True
    return published >= since


def own_run_id(principal: str, workspace: str) -> str | None:
    """The run a ``pipeline:<workspace>:<runId>`` principal names (ADR 0120's run subject), if any."""
    prefix = f"pipeline:{workspace}:"
    return principal[len(prefix) :] if principal.startswith(prefix) and len(principal) > len(prefix) else None


def _fire_at(st: EventTriggerState, t: EventTrigger, now: datetime) -> datetime:
    assert st.pending_since is not None
    at = min(now + timedelta(seconds=t.debounce_seconds), st.pending_since + timedelta(seconds=t.max_wait_seconds))
    if st.last_fired_at is not None:
        at = max(at, st.last_fired_at + timedelta(seconds=t.min_gap_seconds))
    return at


def _add(st: EventTriggerState, ev: TriggerEvent) -> None:
    if len(st.events) < MAX_RUN_EVENTS:
        st.events.append(ev.record())
    st.event_count += 1


# Outcomes of ingest(), for logs and tests.
PENDING, QUEUED, OWN_RUN, SKIPPED = "pending", "queued", "own-run", "skipped"


def ingest(st: EventTriggerState, t: EventTrigger, ev: TriggerEvent, now: datetime, *, own_run: bool, run_active: bool, concurrent: bool) -> str:
    """One matching event. ``own_run``: written by one of this pipeline's runs (the loop guard).
    ``run_active``: the pipeline has an active run right now."""
    if own_run:
        st.ignored_own_runs += 1
        return OWN_RUN
    st.last_event, st.last_event_at = ev.record(), now
    if run_active and not concurrent:
        if t.when_running == "skip":
            st.skipped_while_running += 1
            return SKIPPED
        # queue_one: fold it into the one follow-up; no timer while the run is active.
        _add(st, ev)
        st.queued = True
        return QUEUED
    _add(st, ev)
    # A follow-up owed to a run that has since ended is simply part of this burst now.
    st.queued = False
    st.pending_since = st.pending_since or now
    st.fire_at = _fire_at(st, t, now)
    return PENDING


def on_due(st: EventTriggerState, t: EventTrigger, now: datetime, *, run_active: bool, concurrent: bool) -> dict[str, Any] | None:
    """``fire_at`` has come. Returns the run's ``trigger_events`` if it should start now, else None
    (the state says what happened instead)."""
    if run_active and not concurrent:
        st.pending_since, st.fire_at = None, None
        if t.when_running == "queue_one":
            st.queued = True
        else:
            st.skipped_while_running += st.event_count
            st.events, st.event_count, st.missed_events = [], 0, False
        return None
    fired = {"events": st.events, "count": st.event_count, "missedEvents": st.missed_events}
    st.pending_since, st.fire_at, st.queued = None, None, False
    st.events, st.event_count, st.missed_events = [], 0, False
    st.last_fired_at = now
    return fired


def on_run_finished(st: EventTriggerState, t: EventTrigger, now: datetime) -> None:
    """A run of the pipeline ended: a queued follow-up is due now (or at the minimum gap)."""
    if not st.queued:
        return
    st.queued = False
    st.pending_since = now
    st.fire_at = now if st.last_fired_at is None else max(now, st.last_fired_at + timedelta(seconds=t.min_gap_seconds))


def requeue(st: EventTriggerState, t: EventTrigger, fired: dict[str, Any], now: datetime) -> None:
    """A claimed fire found the pipeline busy after all (a run started between the claim and the
    run's creation): put its events back, and under queue_one owe the follow-up."""
    if t.when_running == "skip":
        st.skipped_while_running += int(fired.get("count") or 0)
        return
    st.events = (list(fired.get("events") or []) + st.events)[:MAX_RUN_EVENTS]
    st.event_count += int(fired.get("count") or 0)
    st.missed_events = st.missed_events or bool(fired.get("missedEvents"))
    st.queued = True
    st.pending_since, st.fire_at = None, None


def on_missed_events(st: EventTriggerState, t: EventTrigger, now: datetime) -> None:
    """The bus may have dropped events (an outage longer than the stream keeps them, ADR 0123): fire
    once, so whatever arrived unseen is picked up by the task's own scan."""
    st.missed_events = True
    st.gap_detected_at = now
    if st.queued or st.fire_at is not None:
        return
    st.pending_since = now
    st.fire_at = now if st.last_fired_at is None else max(now, st.last_fired_at + timedelta(seconds=t.min_gap_seconds))


def reset_pending(st: EventTriggerState) -> None:
    """The trigger was turned off or removed: nothing it was waiting for still fires."""
    st.pending_since, st.fire_at, st.queued = None, None, False
    st.events, st.event_count, st.missed_events = [], 0, False
