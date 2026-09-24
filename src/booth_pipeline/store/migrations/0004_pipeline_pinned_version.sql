-- ADR 0071, "Open question, ruled 2026-09-24" (second one): a scheduled or manual run needs to be
-- able to pin an older pipeline version, the way a Job's `pipeline_version` used to — Pipeline had
-- no equivalent field when phase 2 shipped. NULL means "always run the latest saved version" (the
-- phase 2 default, unchanged); an explicit version pins every run of this pipeline to it until
-- changed. Additive, no backfill: nothing has used pinning since phase 1 removed the Job column
-- that used to hold it.
--
-- The composite FK only fires when pinned_version is set (Postgres's default MATCH SIMPLE treats
-- any-NULL as satisfied) — same shape pipeline_versions' own PK already provides to reference.

ALTER TABLE pipelines ADD COLUMN pinned_version integer;

ALTER TABLE pipelines
    ADD CONSTRAINT pipelines_pinned_version_fkey
    FOREIGN KEY (id, pinned_version) REFERENCES pipeline_versions (pipeline_id, version);
