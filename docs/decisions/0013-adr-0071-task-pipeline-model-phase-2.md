# 0013: Adopting ADR 0071, phase 2 — API + execution path

Status: **phase 2 of 4 built** (API + service business logic + the engine's real resolution-step
work the phase 1 write-up flagged). Phases 3 (builder UI + Task library) and 4 (YAML export) are
not started — sequenced deliberately, per the coordinator's own instruction to check in between
phases.

Both findings phase 1 self-flagged were ruled on by the coordinator before this phase started
(recorded in ADR 0071's own "Correction" and "Open question, ruled 2026-09-24" sections): the
one-schedule-per-pipeline model stands as written, and the engine's resolution-step gap was
confirmed as real, in-scope work for this phase. Both rulings are reflected below.

## What changed

- **`resolve.py`** (new): `resolve_task_configs(store, workspace, spec) -> {ref.key: TaskConfig}`
  — turns a pipeline version's pinned `(taskId, taskVersion)` references back into the configs
  that actually run. Needs no catalog/storage call: a task version's code was already snapshotted
  when that version was itself saved. Shared by the service (save-time compile-check, live
  validation) and the run manager (execution) so both resolve a reference exactly the same way.
- **`engine.py`**: `compile_job`/`execute` now take `(spec, configs)` instead of just `spec` —
  the real, non-trivial signature/data-flow change phase 1 flagged as still outstanding. `_make_op`
  takes a `TaskRef` (wiring) and its resolved `TaskConfig` (execution config) as two separate
  arguments rather than one embedded `Task`. `effective_retry`/`job_retry` are gone outright, not
  just renamed: a task's own `retry` is now the only retry policy there is — no more pipeline-level
  default a task without an override could fall back to, because Pipeline (which absorbed Job's
  fields in phase 1) was never given one. `TaskInvocation.kind` is always `""` now — the field
  stays on the wire (the runner service, its own IPC protocol, and the harness's `ctx.kind` were
  deliberately left untouched; see "Not done" below) but nothing upstream of it carries a `kind`
  worth passing any more.
- **`schemas.py`**: `JobInput` is gone. New `PipelineScheduleUpdate` (schedule, allowConcurrentRuns,
  roleCeiling — everything `JobInput` used to carry that Pipeline now owns directly) and
  `TaskCreate`/`TaskUpdate`/`TaskVersionCreate`, mirroring `PipelineCreate`/`PipelineUpdate`/
  `VersionCreate` exactly.
- **`service.py`**: Job methods are gone; Task CRUD/versioning methods mirror the pipeline/version
  methods precisely. Code resolution (`_fetch_catalog`/`_fetch_storage`) moves from pipeline-save
  time to **task-version**-save time — a pipeline version's own `_prepare` no longer touches the
  catalog or storage at all; it validates DAG shape, pins every `"latest"` reference to a concrete
  version and confirms the referenced task/version actually exists (`_pin_task_versions` — the
  store-lookup phase 1's `validate_structure` explicitly couldn't do), resolves configs via
  `resolve.py`, and compiles. `update_schedule` replaces `create_job`/`update_job`'s
  schedule-setting half. `start_run`/`start_scheduled` take a `Pipeline` instead of a `Job`.
  `delete_pipeline` now cancels the pipeline's own active runs before deleting it — `delete_job`
  used to do exactly this and phase 1's store-layer rewrite of `delete_pipeline` had dropped it
  entirely (a real gap, caught and fixed here; see below).
- **`api.py`**: `/jobs*` routes are gone. New `/tasks` CRUD + versioning routes (mirroring
  booth-catalog's own code-versioning shape, as the brief asked), `PUT
  /pipelines/{id}/schedule`, and `POST /pipelines/{id}/run` (replacing `POST /jobs/{id}/run`).
  `GET /runs` keeps its flat, query-scoped shape (`?pipelineId=` in place of `?jobId=`) rather than
  nesting under `/pipelines/{id}/runs` — satisfies "run-history routes directly on Pipeline" without
  a URL-shape change beyond the rename. `kind` was already gone from the wire in phase 1
  (`TaskConfig`/`TaskRef` never had it); nothing left to remove here.
- **`runs.py`**: `_execute_inner` resolves `configs = resolve_task_configs(...)` before starting a
  run (failing the run cleanly if a reference has gone stale — its task or task version was
  deleted after the pipeline version pointing at it was saved) and reads `role_ceiling` from the
  fetched `Pipeline` instead of a `Job`.
- **`scheduler.py`**: `claim_due_jobs` → `claim_due_pipelines` (phase 1 already did this store-side;
  this phase updates the caller). Mechanics (claim/index cadence split, coalescing, SKIP LOCKED)
  are untouched.
- **`workload.py`**: one wording change — the "no recorded owner" message now says "this pipeline"
  and "save its schedule again" rather than "this job"/"save it again".
- **`app.py`**: `JobBusy` → `PipelineBusy` (renamed, not just moved — "job busy" no longer names
  anything that exists).

## A real gap phase 1 left behind, caught and fixed here

Phase 1's `delete_pipeline` (store layer) dropped the old `InUse`-if-still-referenced check
because there's no more separate Job to raise it — correct, nothing references a pipeline the way
a task can be referenced by a pipeline version. But the *service* layer's old `delete_job` did
something else phase 1's write-up didn't call out: it canceled the job's own active runs before
deleting it, so an orphaned worker wouldn't keep executing with nothing left to record its result
against. `service.delete_pipeline` (this phase's starting point, inherited unchanged from before
ADR 0071) never had this — deleting a pipeline with an active run would have let that run's worker
keep going while its own rows were cascaded out from underneath it by the FK `ON DELETE CASCADE`
phase 1's migration added. Fixed: `delete_pipeline` now cancels the pipeline's active runs first,
the same way `delete_job` always did. Covered by
`test_deleting_a_pipeline_mid_run_stops_the_run` in `test_api.py`.

## A judgment call, self-flagged rather than guessed past

**A scheduled run now always executes the pipeline's latest saved version.** Before ADR 0071, a Job
could pin `pipelineVersion` to a specific number independently of the pipeline's own latest — two
jobs on the same pipeline could genuinely track different versions (`test_a_job_follows_latest_
or_stays_pinned`, now deleted). Phase 1's `Pipeline` dataclass (already shipped, committed in
`2597b26`) has no field for this: only `schedule`/`allowConcurrentRuns`/`nextRunAt`/`ownerSub`/
`roleCeiling` were folded on, matching the *one schedule per pipeline* ruling — but even under that
ruling, the one schedule could in principle still want to pin an older version rather than track
latest. I did not add a `pinnedVersion` field (would need another migration column) and instead
made every run — scheduled or manual — always use the latest saved version, the simplest reading
consistent with "Pipeline owns its own run directly; there is no separate versioned instance to pin
independently." Draw a new pipeline (cheap now that tasks are independently reusable) to keep an
old version scheduled.

Flagging this now rather than deciding it silently: if a real workspace genuinely depends on
pinning a scheduled run to a specific pipeline version while newer versions are drafted, that is a
real gap this model doesn't represent, and would need a new column plus another migration to close
— worth knowing before phase 3 builds a schedule editor UI that has nowhere to put a version picker.

## Verified

- `tests/unit/test_engine.py`: fully rewritten around the new `(spec, configs)` split — a new
  `helpers.build()`/`helpers.node()` pair builds both halves directly (the engine never touches the
  store, so its own tests don't need a real task-creation round trip). 24 tests, all passing. The
  old `test_job_default_retry_applies_to_tasks_without_an_override_and_the_override_wins`/
  `test_effective_retry_precedence` are gone (nothing left to test — there is no more job-level
  default); replaced by `test_a_tasks_own_retry_is_all_there_is_no_pipeline_level_default`.
- `tests/unit/test_api.py`: fully rewritten. `harness.py`'s `Env.call` now materializes a
  pipeline-body's `spec.tasks` — each node built with the (unchanged-shape, `kind`-argument-dropped)
  `task()` helper is turned into a real standalone Task via `POST /tasks` (always under a
  write-capable token, since this is test setup, not what a test is exercising) before the outer
  call goes out, so most call sites needed no restructuring beyond dropping the retired `kind`
  argument. Catalog/storage resolution tests moved to the task level, where that behavior now
  actually lives; a new `test_a_pipeline_version_needs_no_catalog_or_storage_call_at_all` proves the
  pipeline-version save path is now *unconditionally* independent of catalog/storage reachability,
  not just when reusing a previous reference. New task CRUD/versioning/`InUse`-while-referenced
  coverage mirroring the pipeline/version tests. 54 tests, all passing.
- `tests/unit/test_scheduler.py`, `test_workload_e2e.py`, `test_workload.py`,
  `test_workload_model.py`, `test_recorder.py`, `test_trigger_backcompat.py`, `test_app.py`: all
  rewritten to the new pipeline-owns-its-own-schedule shape. 15 + 10 + 20 + 9 + 6 + 1 + 7 tests,
  all passing.
- Full suite (`pytest tests/unit`, real Postgres via `hack/docker-compose.test.yml`): every test
  file collects and passes — the 9 files phase 1 left failing to collect are the ones this phase
  rewrote. `ruff check` clean on every file this phase touched, `src/` and `tests/unit/` both.

## Not done (by design)

- The runner service's own IPC protocol (`runner_service.py`'s `RunBody.kind`,
  `subprocess_runner.py`/`remote.py`'s `inv.kind` payload field, `_harness.py`'s `ctx.kind`) still
  carries a `kind` field end to end — now always `""`, since nothing upstream sets anything else.
  Ripping it out of that separate wire protocol is orthogonal to the ADR 0071 model change (it was
  already purely decorative under ADR 0062) and not worth the blast radius across a second service
  boundary for this phase; flagging it here in case a future cleanup wants to finish the job.
- `api.py`/`web/*`: the frontend (`PipelineApp.tsx`, `JobViews.tsx`, `DagCanvas.tsx`, etc.) still
  targets the retired `/jobs` routes and the old embedded-task wire shape — it will not build
  against this backend until phase 3 replaces it. Expected; phase 3's own scope.
- YAML export. Phase 4.
