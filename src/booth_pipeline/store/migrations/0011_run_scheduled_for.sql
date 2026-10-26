-- A scheduled run's slot (the claimed next_run_at), so a late start still knows which slot it is for
-- (ctx.trigger.scheduledFor). Null for manual and event-fired runs, and for runs from before this.
-- No new status constraint: runs.status has none, and "skipped" (ctx.skip, or a dropped scheduled
-- fire) is just another terminal value.
ALTER TABLE runs ADD COLUMN scheduled_for timestamptz;
