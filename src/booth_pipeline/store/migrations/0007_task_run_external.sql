-- ADR 0119: a task attempt that ran somewhere else (a booth-spark application) records it here, so the
-- run view can show its state and link to it: {kind, id, state, reason}. NULL for every other task.
ALTER TABLE task_runs ADD COLUMN external jsonb;
