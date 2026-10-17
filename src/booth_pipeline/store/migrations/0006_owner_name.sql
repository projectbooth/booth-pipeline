-- ADR 0119 amendment 3 / ADR 0120: who a pipeline runs as, said in words. `owner_sub` keeps the id
-- core mints for (a person's `sub`, or a service account `svc:<workspace>:<name>`); `owner_name` is
-- how the UI names it, taken when the owner was set (the person directory is core's, ADR 0047).
-- Existing owners get an empty name: the UI says "the person who first scheduled it".
ALTER TABLE pipelines ADD COLUMN owner_name text NOT NULL DEFAULT '';
-- A run records who it acted as: the pipeline's owner for a scheduled run, the person who pressed
-- Run now for a manual one.
ALTER TABLE runs ADD COLUMN owner_name text NOT NULL DEFAULT '';
