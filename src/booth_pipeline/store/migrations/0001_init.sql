-- booth-pipeline's schema. Lives in this module's own database on the shared PostgreSQL
-- cluster (ADR 0014/0053); core provisions the database and role, the schema is ours.
--
-- IDs are text (uuid4 strings minted by the service) so a malformed id in a URL is simply "not
-- found" rather than a database type error. Every table carries or reaches a `workspace` and every
-- query filters on it (ADR 0008).

CREATE TABLE pipelines (
    id          text PRIMARY KEY,
    workspace   text NOT NULL,
    name        text NOT NULL,
    description text NOT NULL DEFAULT '',
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL,
    UNIQUE (workspace, name)
);

-- One immutable row per save. The spec (graph + snapshotted code) is one document: it is always
-- read and written whole, and never queried into.
CREATE TABLE pipeline_versions (
    pipeline_id text NOT NULL REFERENCES pipelines (id) ON DELETE CASCADE,
    version     integer NOT NULL,
    spec        jsonb NOT NULL,
    notes       text NOT NULL DEFAULT '',
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL,
    PRIMARY KEY (pipeline_id, version)
);

-- No ON DELETE CASCADE from jobs to pipelines: deleting a pipeline that has jobs is refused
-- (409), not silently taking the jobs and their run history with it.
CREATE TABLE jobs (
    id                    text PRIMARY KEY,
    workspace             text NOT NULL,
    name                  text NOT NULL,
    pipeline_id           text NOT NULL REFERENCES pipelines (id),
    pipeline_version      integer,           -- NULL = follow the pipeline's latest version
    schedule              jsonb,             -- {cron, timezone, enabled} or NULL
    retry                 jsonb,             -- default retry policy for tasks without an override
    allow_concurrent_runs boolean NOT NULL DEFAULT false,
    created_by            text NOT NULL,
    created_at            timestamptz NOT NULL,
    updated_at            timestamptz NOT NULL,
    next_run_at           timestamptz,
    UNIQUE (workspace, name)
);
-- The scheduler's one hot query: what is due?
CREATE INDEX jobs_due ON jobs (next_run_at) WHERE next_run_at IS NOT NULL;

CREATE TABLE runs (
    id                text PRIMARY KEY,
    workspace         text NOT NULL,
    job_id            text NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    pipeline_id       text NOT NULL,
    pipeline_version  integer NOT NULL,
    status            text NOT NULL,
    trigger           text NOT NULL,
    triggered_by      text NOT NULL,
    created_at        timestamptz NOT NULL,
    started_at        timestamptz,
    finished_at       timestamptz,
    error             text,
    worker_id         text,
    heartbeat_at      timestamptz,
    cancel_requested  boolean NOT NULL DEFAULT false
);
CREATE INDEX runs_by_job ON runs (workspace, job_id, created_at DESC);
CREATE INDEX runs_active ON runs (status) WHERE status IN ('queued', 'running');

CREATE TABLE task_runs (
    run_id      text NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    task_key    text NOT NULL,
    status      text NOT NULL,
    attempts    integer NOT NULL DEFAULT 0,
    started_at  timestamptz,
    finished_at timestamptz,
    error       text,
    PRIMARY KEY (run_id, task_key)
);

-- Run logs. `seq` is assigned per run by the single worker that owns it, so (run_id, seq) is both
-- the primary key and the cursor a client polls with ("everything after seq N").
CREATE TABLE run_logs (
    run_id   text NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    seq      bigint NOT NULL,
    ts       timestamptz NOT NULL,
    task_key text,
    attempt  integer NOT NULL DEFAULT 0,
    stream   text NOT NULL,
    message  text NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX run_logs_by_task ON run_logs (run_id, task_key, seq);
