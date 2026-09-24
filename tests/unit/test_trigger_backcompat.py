"""ADR 0065's back-compat promise, proven against a real database row: every pipeline saved before
the ``Trigger`` union existed has a ``schedule`` JSONB value shaped like the old ``Schedule`` model
— ``{cron, timezone, enabled}``, no ``type`` key. It must keep loading, unchanged, as a
``CronTrigger``, with no migration. This is deliberately NOT exercised through our own store-write
code (which always writes a real pydantic model, and so always includes ``type``) — the only way
this shape exists is a pre-existing row, so the test plants one directly."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from psycopg.types.json import Jsonb

from booth_pipeline.model import CronTrigger, PipelineSpec
from booth_pipeline.schedule import next_fire_trigger

WS = "acme"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_a_legacy_bare_cron_schedule_row_loads_as_a_crontrigger_and_still_schedules(pg_store):
    store = pg_store
    with store._pool.connection() as conn:
        conn.execute("TRUNCATE run_logs, task_runs, runs, pipeline_versions, pipelines, task_versions, tasks")
    p = store.create_pipeline(WS, f"etl-{uuid4().hex[:6]}", "", "a")
    store.add_version(WS, p.id, PipelineSpec(), "", "a")

    # Plant the pre-0065 shape directly — no "type" key — exactly what a real row looked like.
    legacy = {"cron": "0 9 * * *", "timezone": "America/Toronto", "enabled": True}
    with store._pool.connection() as conn:
        conn.execute("UPDATE pipelines SET schedule = %s WHERE id = %s", (Jsonb(legacy), p.id))

    got = store.get_pipeline(WS, p.id)
    assert isinstance(got.schedule, CronTrigger)
    assert (got.schedule.type, got.schedule.cron, got.schedule.timezone, got.schedule.enabled) == ("cron", "0 9 * * *", "America/Toronto", True)

    # and it schedules exactly as it always did — next_fire_trigger doesn't know or care that this
    # row predates the Trigger union.
    assert next_fire_trigger(got.schedule, T0) == datetime(2026, 1, 1, 14, 0, tzinfo=UTC)  # 09:00 America/Toronto in January = UTC-5

    # claim_due_pipelines (the real scheduler path) also loads and reschedules it correctly.
    with store._pool.connection() as conn:
        conn.execute("UPDATE pipelines SET next_run_at = %s WHERE id = %s", (T0, p.id))
    (claimed,) = store.claim_due_pipelines(T0, 10)
    assert claimed.id == p.id and isinstance(claimed.schedule, CronTrigger)
    assert store.get_pipeline(WS, p.id).next_run_at == datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
