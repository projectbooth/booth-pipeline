-- ADR 0071: Task becomes a standalone, independently versioned resource; Job is retired and its
-- fields fold directly onto Pipeline. This is a real, one-time data migration, not just a schema
-- change: every pipeline_versions row's embedded task objects become new standalone Task entities
-- (one first version each), the pipeline version is rewritten to reference them, and each job's
-- schedule/ownership/run-concurrency settings move onto its pipeline.
--
-- A pipeline can have MORE THAN ONE job today (no uniqueness constraint ties jobs.pipeline_id to
-- one row) — the new model has room for exactly one schedule per pipeline. Where a pipeline has
-- several jobs, this migration folds the one with the SOONEST next_run_at (ties broken by
-- created_at, then id) onto the pipeline directly; the others' schedules are not carried forward.
-- Every job's run history is preserved regardless (runs.pipeline_id already names the right
-- pipeline; only the now-redundant job_id column is dropped), so nothing about past runs is lost —
-- only which of several jobs a workspace has to re-create a second schedule for, if it had more
-- than one on the same pipeline. Self-flagged: see docs/decisions for the ADR 0071 adoption note.

-- ---- tasks: the direct counterpart of pipelines/pipeline_versions ----------------------------

CREATE TABLE tasks (
    id          text PRIMARY KEY,
    workspace   text NOT NULL,
    name        text NOT NULL,
    description text NOT NULL DEFAULT '',
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL
    -- Deliberately no UNIQUE(workspace, name): unlike a Pipeline, a Task's name is not an
    -- identifier a person types to navigate to it — it's a label in a browsable library, and this
    -- migration alone will mint many same-named tasks (one per pre-existing embedded task).
);

CREATE TABLE task_versions (
    task_id     text NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    version     integer NOT NULL,
    config      jsonb NOT NULL,
    notes       text NOT NULL DEFAULT '',
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL,
    PRIMARY KEY (task_id, version)
);

-- ---- pipeline absorbs job's fields directly (ADR 0071: no more separate Job entity) -----------

ALTER TABLE pipelines ADD COLUMN schedule jsonb;
ALTER TABLE pipelines ADD COLUMN allow_concurrent_runs boolean NOT NULL DEFAULT false;
ALTER TABLE pipelines ADD COLUMN next_run_at timestamptz;
ALTER TABLE pipelines ADD COLUMN owner_sub text NOT NULL DEFAULT '';
ALTER TABLE pipelines ADD COLUMN role_ceiling text NOT NULL DEFAULT 'editor';
-- The scheduler's one hot query: what is due? (was jobs_due)
CREATE INDEX pipelines_due ON pipelines (next_run_at) WHERE next_run_at IS NOT NULL;

-- ---- data migration: rewrite every pipeline version's embedded tasks into Task references ------
-- A PL/pgSQL block, not a declarative query: minting a fresh id per embedded task and rewriting
-- the containing JSONB document is much easier to get right and audit this way than as one giant
-- correlated JSONB query, and this runs exactly once, over a bounded amount of existing data.

DO $migrate_tasks$
DECLARE
    pv          RECORD;
    old_task    jsonb;
    new_task_id text;
    new_refs    jsonb;
    ref         jsonb;
BEGIN
    FOR pv IN SELECT v.pipeline_id, v.version, v.spec, v.created_by, v.created_at, p.workspace
              FROM pipeline_versions v JOIN pipelines p ON p.id = v.pipeline_id
    LOOP
        new_refs := '[]'::jsonb;
        FOR old_task IN SELECT * FROM jsonb_array_elements(COALESCE(pv.spec->'tasks', '[]'::jsonb))
        LOOP
            new_task_id := gen_random_uuid()::text;

            INSERT INTO tasks (id, workspace, name, description, created_by, created_at, updated_at)
            VALUES (
                new_task_id, pv.workspace,
                COALESCE(NULLIF(old_task->>'name', ''), old_task->>'key'),
                '', pv.created_by, pv.created_at, pv.created_at
            );

            -- `kind`, `key`, `dependsOn`, `position` and `name` are dropped here: the first three
            -- move to the TaskRef below, `name` became the new Task's own name above, and `kind`
            -- is retired outright (ADR 0071) — it was already purely decorative (ADR 0062).
            INSERT INTO task_versions (task_id, version, config, notes, created_by, created_at)
            VALUES (
                new_task_id, 1,
                jsonb_build_object(
                    'code', old_task->'code',
                    'runner', COALESCE(old_task->>'runner', 'base'),
                    'retry', old_task->'retry',
                    'params', COALESCE(old_task->'params', '{}'::jsonb),
                    'timeoutSeconds', COALESCE(old_task->'timeoutSeconds', to_jsonb(3600)),
                    'platformAccess', COALESCE(old_task->'platformAccess', to_jsonb(false))
                ),
                '', pv.created_by, pv.created_at
            );

            ref := jsonb_build_object(
                'key', old_task->'key',
                'taskId', new_task_id,
                'taskVersion', 1,
                'dependsOn', COALESCE(old_task->'dependsOn', '[]'::jsonb),
                'position', COALESCE(old_task->'position', jsonb_build_object('x', 0, 'y', 0))
            );
            new_refs := new_refs || jsonb_build_array(ref);
        END LOOP;

        UPDATE pipeline_versions SET spec = jsonb_build_object('tasks', new_refs)
        WHERE pipeline_id = pv.pipeline_id AND version = pv.version;
    END LOOP;
END;
$migrate_tasks$;

-- ---- data migration: fold each pipeline's chosen job onto it, then retire the job table --------

DO $migrate_jobs$
DECLARE
    chosen RECORD;
BEGIN
    FOR chosen IN
        SELECT DISTINCT ON (j.pipeline_id)
            j.pipeline_id, j.schedule, j.allow_concurrent_runs, j.next_run_at, j.owner_sub, j.role_ceiling
        FROM jobs j
        ORDER BY j.pipeline_id, j.next_run_at ASC NULLS LAST, j.created_at ASC, j.id ASC
    LOOP
        UPDATE pipelines SET
            schedule = chosen.schedule,
            allow_concurrent_runs = chosen.allow_concurrent_runs,
            next_run_at = chosen.next_run_at,
            owner_sub = chosen.owner_sub,
            role_ceiling = chosen.role_ceiling
        WHERE id = chosen.pipeline_id;
    END LOOP;
END;
$migrate_jobs$;

-- ---- runs move from job-owned to pipeline-owned directly ---------------------------------------

ALTER TABLE runs DROP CONSTRAINT runs_job_id_fkey;
DROP INDEX runs_by_job;
ALTER TABLE runs DROP COLUMN job_id;
ALTER TABLE runs ADD CONSTRAINT runs_pipeline_id_fkey FOREIGN KEY (pipeline_id) REFERENCES pipelines (id) ON DELETE CASCADE;
CREATE INDEX runs_by_pipeline ON runs (workspace, pipeline_id, created_at DESC);

DROP TABLE jobs;
