-- Run retention (execution.retention.days): the nightly prune takes the oldest finished runs a batch at a
-- time. Only finished runs have finished_at, so the index is just those.
CREATE INDEX runs_finished_at ON runs (finished_at) WHERE finished_at IS NOT NULL;
