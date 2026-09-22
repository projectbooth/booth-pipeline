# 0009: Adopting ADR 0065 — friendlier scheduling UI + sub-minute intervals

Status: **built.**

## What changed

- `model.py`: `Schedule` was renamed `CronTrigger` (fields and validation unchanged) and a new
  `IntervalTrigger` (`type: "interval"`, `seconds: int` bounded to `[MIN_INTERVAL_SECONDS,
  MAX_INTERVAL_SECONDS]` = `[5, 30 days]`, `enabled: bool`) was added alongside it.
  `Trigger = Annotated[CronTrigger | IntervalTrigger, Field(discriminator="type"), BeforeValidator]`
  is the new wire type for `Job.schedule` / `JobInput.schedule`. The `BeforeValidator` injects
  `type: "cron"` into any bare `{cron, timezone, enabled}` dict with no discriminator — the exact
  shape every job saved before this ADR still has in the database — so no migration is needed.
  `Schedule` is kept as a deprecated alias for `CronTrigger`.
- `MIN_INTERVAL_SECONDS = 5` was chosen empirically, not picked arbitrarily: a spike script
  (`/tmp/spike_timing.py`, not committed) measured ~0.17-0.226s wall-clock for a trivial
  single-task run end to end (queue → base runner → done) against this repo's own dev stack, so 5s
  gives a real safety margin above the shortest a run can plausibly take, rather than inviting a
  job to reschedule itself into an overlapping run.
- `schedule.py`: added `next_fire_trigger(trigger, after)`, generalizing `next_fire` over the
  `Trigger` union — a `CronTrigger` behaves exactly as before; an `IntervalTrigger` simply adds
  `seconds` to `after`, with no calendar alignment (firing again from the fire time just adds the
  interval again, so a paused-then-resumed interval doesn't try to "catch up" to a wall-clock
  grid the way a cron schedule's missed-fire coalescing does).
- `store/postgres.py`, `store/memory.py`: the schedule column/field is now decoded with
  `TypeAdapter(Trigger).validate_python(...)` instead of `Schedule.model_validate(...)` — the
  latter would crash on a stored `IntervalTrigger` row, since `Schedule` is just `CronTrigger`
  under a different name now.
- `scheduler.py` (the bigger piece): rewritten from a fixed-interval poll to an index-driven loop,
  per the ADR's explicit recommendation — a poll cranked down to a few seconds across every
  replica would otherwise hammer the database with the locking `claim_due_jobs` query even when
  nothing is due. The loop now separates a cheap, non-locking read (`store.list_upcoming`, on a
  coarser refresh cadence) from the locking claim write (`store.claim_due_jobs`, only invoked when
  the in-memory index says something is actually due). Sleep between iterations is computed from
  the nearer of "next index refresh" and "next known fire," so a short interval job still fires
  promptly even with a long refresh cadence, and the index re-checks immediately after a claim so
  a job firing every few seconds isn't throttled to the refresh cadence. The existing
  atomic-claim-across-replicas (`SELECT ... FOR UPDATE SKIP LOCKED`) and missed-fire-coalescing
  behavior are both untouched — this only changes when the claim query runs, not what it does.
  `store/base.py` gained `list_upcoming(limit)` as the new read-only counterpart to
  `claim_due_jobs`.
- `web/src/types.ts`, `web/src/format.ts`: mirrored the `CronTrigger`/`IntervalTrigger`/`Trigger`
  shapes; `describeSchedule` now branches on `type` (`describeInterval(seconds)` renders "every 30
  seconds" / "every 5 minutes" / "every 2 hours" using whichever unit divides evenly).
- `web/src/components/ScheduleEditor.tsx` (new): the friendlier point-and-click layer the ADR
  asked for. A "Frequency" picker (Daily / Weekly / Every N hours / Every N minutes / Every N
  seconds / Custom) maps onto either a plain cron string or an `IntervalTrigger` — the wire format
  gains no "frequency" concept of its own, this is presentation only. Opening an existing job
  parses its saved cron expression back into the friendly shape where it recognizably fits
  (`parseCronFriendly`: a fixed minute+hour with `dow=*` is "Daily," a fixed single weekday is
  "Weekly," `*/N` in the hour or minute field is "Every N hours/minutes"); anything else — an
  arbitrary hand-written cron, a day-of-month restriction, `dow` ranges/lists — falls back to
  "Custom," which edits the raw expression directly, so nothing a user or `booth-e2e` already
  saved is ever misrepresented or silently rewritten. "Every N seconds" has no timezone field
  (an interval has none to keep); the other five do.
- `web/src/views/JobViews.tsx`: `JobForm`'s schedule section now renders `ScheduleEditor` in place
  of the old bare cron/timezone inputs; the "Schedule is active" pause checkbox is unchanged.

## Verified

- `test_model.py`: a bare pre-0065 dict still loads as `CronTrigger` via the coercing validator
  (and an explicit `type: "cron"` dict is untouched by it); `IntervalTrigger`'s bounds
  (`MIN_INTERVAL_SECONDS`, `MAX_INTERVAL_SECONDS`, one below/above each); an unknown `type` is
  rejected, not silently dropped; `schedule` stays optional on a job; `next_fire_trigger` dispatches
  correctly for both members and requires a timezone-aware `after` for either.
- `test_trigger_backcompat.py` (new): plants the literal pre-0065 JSONB shape directly into a real
  Postgres `jobs.schedule` column (not through our own store-write code, which always includes
  `type`) and proves `get_job`, `next_fire_trigger`, and `claim_due_jobs` all still handle it
  correctly as a `CronTrigger`.
- `test_scheduler.py`: an interval job fires via `tick()` and reschedules by simple addition from
  the actual fire time (not the stale `next_run_at`); an interval below `MIN_INTERVAL_SECONDS` is
  rejected with `field == "schedule.interval.seconds"`. Four new loop-behavior tests exercise real
  wall-clock timing against the redesigned `_loop()`: no claim query runs when nothing is due
  (checked by spying on `store.claim_due_jobs`); a due job fires promptly even with a 30s refresh
  cadence; a short interval keeps firing at its own pace rather than being throttled to a 60s
  refresh cadence; a job created after the loop started is still picked up within one refresh
  cycle. Confirmed stable across 3 repeated runs (~8.1s each, no flakes).
- `ScheduleEditor.test.tsx` (new): each friendly frequency round-trips against the cron/interval
  shape it maps to and from (daily, weekly, hourly, minutes, seconds); a cron shape none of the
  friendly pickers can express falls back to "Custom" and is never lost or rewritten; switching
  frequency between a calendar option and "seconds" emits the correct trigger shape.
- `PipelineApp.test.tsx`: a job created via the "Custom" cron path still sends the exact expected
  wire body; a new test creates a job on a plain "Every N seconds" interval end to end and asserts
  no timezone field is shown and the submitted body matches `{type: "interval", seconds: 5,
  enabled: true}`.
- Full suites green: 273 Python (`pytest tests/unit tests/contract`, real Postgres via
  `hack/docker-compose.test.yml`), `ruff check` clean, 119 web (`vitest`), `tsc --noEmit` clean,
  `eslint` clean, `vite build` clean.

## Not done

- The friendly picker's "Weekly" mode is a single weekday, matching the ADR's own example ("every
  Monday"); a multi-day set (e.g. "weekdays") still round-trips correctly but only through
  "Custom" — it was not given its own friendly control, since the ADR didn't ask for one and the
  existing `0 9 * * 1-5` preset already covers the common case via the datalist.
- No new floor was placed on how *often* an interval-triggered job's own run can safely be
  scheduled below `MIN_INTERVAL_SECONDS` beyond the bound already in `model.py`; the timing spike
  that justified `5` was measured against this repo's own dev stack, not booth-e2e's, and is noted
  here as the assumption to revisit if a heavier smoke-test workload needs it lowered or raised.
