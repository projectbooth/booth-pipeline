# 0012: Adopting ADR 0071, phase 1 — data model + migration

Status: **phase 1 of 4 built** (data model + migration + store layer). Phases 2 (API), 3 (builder
UI + Task library), 4 (YAML export) are not started — sequenced deliberately, per the coordinator's
own instruction to check in between phases rather than attempt this in one pass.

## What changed

- **`model.py`**: `Task` (embedded, 1:1 with a pipeline) is gone, replaced by two types:
  - `TaskConfig` — one version's worth of a standalone Task's configuration (`code`, `runner`,
    `retry`, `params`, `timeoutSeconds`, `platformAccess`). No `key`, `dependsOn`, `position` or
    `kind` — those describe how a *reference* to a task is wired into a pipeline, not anything
    about the task itself. `kind` is gone outright (not merely optional, per ADR 0071's own
    amendment to ADR 0062).
  - `TaskRef` — one DAG node: `{key, taskId, taskVersion, dependsOn, position}`. `taskVersion`
    accepts `"latest"` at authoring time and is pinned to a concrete version at save time, the
    same pattern `CatalogCode.version` already established (ADR 0010).
  - `validate_structure(spec)` lost its `available_runners` parameter and the pipeline-wide
    `MAX_SPEC_SOURCE_BYTES` check — both moved to a new `validate_task_config(config,
    available_runners=None)`, since runner availability and a task's own snapshotted-source size
    are now properties of the Task being referenced, not of any pipeline that references it.
    `validate_structure` is now purely DAG-shape (cycle, self-loop, duplicate key/edge, dependency
    resolves within the same spec) — it cannot and does not check that a referenced task actually
    exists (needs a store lookup; that's phase 2, at save time).
- **`records.py`**: `Job` is gone. `Pipeline` gains `schedule`, `allowConcurrentRuns`,
  `nextRunAt`, `ownerSub`, `roleCeiling` — everything Job used to hold, folded on directly (ADR
  0071: Pipeline is now the only thing that ever runs unattended). New `TaskEntity` /
  `TaskVersionRecord`, the direct counterparts of `Pipeline` / `PipelineVersion`. `Run` loses
  `job_id` (redundant once Pipeline owns run history directly — `pipeline_id` already named the
  right thing).
- **`store/base.py`, `store/memory.py`, `store/postgres.py`**: the `Store` protocol's job methods
  (`create_job`/`get_job`/`list_jobs`/`update_job`/`delete_job`) are replaced by a new
  `update_schedule(pipeline)` (mirrors how `add_version` is its own call, separate from
  `create_pipeline`) and a full task CRUD+versioning surface mirroring pipelines/versions exactly
  (`create_task`, `get_task`, `list_tasks`, `update_task`, `delete_task`, `add_task_version`,
  `get_task_version`, `list_task_versions`). `claim_due_jobs`/`list_upcoming` retarget to
  `claim_due_pipelines`/`list_upcoming` (same name, now Pipeline-scoped) — the SKIP LOCKED claim
  mechanics, missed-fire coalescing and multi-replica-safety are byte-for-byte unchanged, just
  querying `pipelines` instead of `jobs`. `create_run`'s exclusivity check and
  `count_active_runs`/`list_runs` retarget from `job_id` to `pipeline_id`.
- **SQL migration** (`0003_task_pipeline_model.sql`): new `tasks`/`task_versions` tables (no
  `UNIQUE(workspace, name)` on `tasks` — unlike Pipeline, a Task's name is a browsable label, not
  a navigation identifier, and this very migration mints many same-named tasks). `pipelines` gains
  the folded-on Job columns plus a `pipelines_due` index. `runs.job_id` is dropped;
  `runs.pipeline_id` gets a real `ON DELETE CASCADE` foreign key for the first time (it was a bare
  text column before — `job_id` was the one with the FK, since Job used to be the thing that
  blocked/cascaded deletion). Deleting a pipeline now cascades its own runs directly, the same way
  `pipeline_versions` already did — there is no more separate Job to block deletion, and nothing
  else needs to.
  - **The actual data rewrite is a PL/pgSQL `DO` block**, not a declarative query: minting a fresh
    id per embedded task and rewriting the containing JSONB document (dropping `kind`/`key`
    /`dependsOn`/`position`/`name` into their new homes) is far easier to get right and audit this
    way than as one giant correlated JSONB query, and this runs exactly once over a bounded amount
    of existing data. Verified against real seeded pre-migration data (not just read for
    plausibility): a two-version pipeline whose versions reused the same task key,
    catalog-referenced code, retry/params/timeout/platformAccess all present, all came out with
    the expected task/task_version/rewritten-spec shape.

## A real judgment call, self-flagged rather than guessed past

**A pipeline can have more than one job today** — `jobs.pipeline_id` has no uniqueness constraint,
and the existing UI (`JobViews.tsx`'s own subtitle) explicitly advertises "the same pipeline can
back several jobs, each with its own schedule." The new model has room for exactly one schedule
per pipeline, so folding N jobs onto one pipeline can't preserve all N schedules — there is no
migration strategy that avoids this; it's an inherent consequence of the model ADR 0071 chose, not
a migration bug.

**What I did**: for a pipeline with multiple jobs, the migration folds the one with the **soonest
`next_run_at`** onto the pipeline (ties broken by `created_at`, then `id`); the others' schedules
are not carried forward. Every job's **run history is fully preserved regardless** — `runs
.pipeline_id` already named the right pipeline before this migration touched anything, so nothing
about past runs is lost, only which of several jobs a workspace would need to re-create a second
schedule for, if it genuinely had more than one active schedule on the same pipeline. Verified
directly against seeded data (a pipeline with two jobs, one due sooner than the other): the sooner
one's schedule/owner/role-ceiling/concurrency settings won.

I did not check with the coordinator before choosing this before writing it, per the standing
instruction to flag rather than block — but flagging it clearly now: if any real workspace turns
out to depend on a pipeline having multiple independent schedules, that's a real gap this model
cannot represent at all (not just a migration nuance), and is worth surfacing before phase 2 makes
it load-bearing in the API.

## Also worth surfacing now, before phase 2

**`engine.py`'s `compile_job`/`execute` still take a `PipelineSpec` expecting embedded `Task`
objects** (`task.code`, `task.runner`, `task.retry`, etc. read directly off each spec entry). Now
that `PipelineSpec.tasks` are references, not embedded configs, phase 2 will need to change
`engine.py`'s own signature to accept something that pairs each `TaskRef` with its resolved
`TaskConfig` (the service will need to resolve `(task_id, task_version)` → `TaskConfig` for every
node before compiling). This is real, non-trivial work the ADR's consequence section doesn't
mention: "the engine already treats any-size DAGs uniformly... no new execution-path work needed"
is true for DAG *topology* (single-node vs. multi-node), but not for how a task's own execution
config reaches the engine — that path assumed embedded tasks throughout and will need to change.
Flagging this now so it isn't a surprise when phase 2 starts.

## Verified

- `test_model.py`: fully rewritten — `TaskRef`'s `"latest"`/pinned/`resolved` behavior, `kind`
  rejected outright as an unrecognised field on both `TaskRef` and `TaskConfig`,
  `validate_structure` no longer touches runner availability or byte size (a new test proves it
  doesn't even notice a nonexistent `taskId`), `validate_task_config`'s runner-availability and
  source-size checks moved intact. 40 tests, all passing.
- `test_store_contract.py`: fully rewritten, run against **both** `MemoryStore` and
  `PostgresStore` (the existing contract-test philosophy — "behaviourally identical" is the
  definition of correct here). New sections for task CRUD/versioning/`InUse`-while-referenced
  (mirroring the pipeline/version tests exactly) and pipeline scheduling
  (`update_schedule`/`claim_due_pipelines`/`list_upcoming`, mirroring the old job tests). Runs
  retarget to `pipeline_id` throughout; a new test proves deleting a pipeline cascades its runs.
  52 tests, all passing against both stores.
- The migration itself verified empirically against a real, freshly-seeded pre-migration database
  (not just read for plausibility): a multi-version pipeline with reused task keys and real
  catalog-referenced code, a pipeline with two jobs (proving the soonest-wins fold), a pipeline
  with one job (the simple 1:1 case), and a run with logs (proving the pipeline-owns-runs cascade
  survives the migration). `PostgresStore.migrate()` itself verified to run cleanly from scratch
  (0001→0002→0003) and to be idempotent on a second call.
- A real bug caught by this same empirical testing, not by inspection: `delete_task`'s first draft
  relied on a foreign-key violation to detect "still referenced" (copying `delete_pipeline`'s old
  pattern), but a `TaskRef`'s `taskId` lives inside a JSONB column, not a real SQL column — no FK
  can ever fire. Fixed to an explicit `EXISTS (... jsonb_array_elements ...)` check; re-verified
  against real seeded data (raised `InUse` while referenced, succeeded once the referencing
  pipeline was removed).
- `ruff check` clean on every file this phase touched.
- **Not run**: the full `pytest tests/unit tests/contract` suite does not pass as a whole — 9
  files (`test_api.py`, `test_engine.py`, `test_scheduler.py`, `test_workload*.py`,
  `test_trigger_backcompat.py`, `test_recorder.py`, `test_app.py`, and their shared
  `harness.py`/`conftest.py` fixtures) fail to even *collect*, since `api.py`/`service.py`/
  `engine.py`/`scheduler.py` still import the now-retired `Job`/`Task` types. This is expected and
  scoped: those files are phase 2's responsibility (API + service business logic), not phase 1's
  (data model + migration + store). Confirmed each failure is a clean `ImportError` naming exactly
  `Job` or `Task` — nothing more subtle hiding behind it.

## Not done (by design — later phases)

- `api.py`/`service.py`/`schemas.py`: still reference the retired `Job`/old `Task` types; do not
  import. Phase 2.
- `engine.py`: still expects embedded `Task` objects on a spec (see the flagged note above). Phase
  2.
- `scheduler.py`/`runs.py`/`workload.py`: still call the old `claim_due_jobs`/job-scoped store
  methods by name; need retargeting onto `claim_due_pipelines` and pipeline-scoped ownership.
  Phase 2.
- Everything UI-facing (Task library/browsing view, `DagCanvas`'s `kind` styling removal, the
  starter-button replacement, relocating `ScheduleEditor` onto Pipeline). Phase 3.
- YAML export. Phase 4.
