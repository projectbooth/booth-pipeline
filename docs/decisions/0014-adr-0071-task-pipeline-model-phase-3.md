# 0014: Adopting ADR 0071, phase 3 — builder UI + Task library

Status: **phase 3 of 4 built** (builder UI + Task library + Pipeline schedule UI), plus a small
backend addition (`pinnedVersion`) that phase 2's own report flagged as needing to land before this
phase, since the schedule-editor UI needs a version picker. Phase 4 (YAML export) is not started.

## Backend addition landed first: `Pipeline.pinnedVersion`

ADR 0071's second "Open question, ruled 2026-09-24" (recorded in the phase 2 report) ruled: add a
nullable `pinnedVersion: int | None` to `Pipeline`, mirroring the old Job's `pipeline_version` pin.
Landed ahead of the UI work as its own commit: an additive migration
(`0004_pipeline_pinned_version.sql`, a nullable column plus a composite FK to
`pipeline_versions(pipeline_id, version)` that only fires when set), `PipelineScheduleUpdate`
gains the field, `service.update_schedule` validates it references a real saved version,
`service.start_run` resolves `pipeline.pinned_version` instead of unconditionally `None`. Verified:
new store-contract test (`test_pinned_version_is_persisted_and_can_be_cleared`), new API tests
(`test_a_pinned_version_stays_pinned_even_as_newer_versions_are_saved`,
`test_pinning_a_version_that_does_not_exist_is_422`), full suite still green against real Postgres.

## What changed (frontend)

- **`types.ts`**: `Task` splits into `TaskRef` (DAG-node wiring: key/taskId/taskVersion/dependsOn/
  position) and `TaskConfig` (code/runner/retry/params/timeoutSeconds/platformAccess) plus
  `TaskEntity`/`TaskVersion` for the standalone resource. `Job`/`JobInput` are gone; `Pipeline`
  gains `schedule`/`allowConcurrentRuns`/`roleCeiling`/`hasOwner`/`nextRunAt`/`pinnedVersion`
  directly. `TaskKind` is gone outright.
- **`api/client.ts`**: `/jobs*` methods replaced by `updateSchedule`/`runPipeline` on Pipeline and a
  full `listTasks`/`createTask`/`updateTask`/`deleteTask`/`*TaskVersion*` surface mirroring the
  pipeline/version methods.
- **`navigation.ts`**: `jobs`/`job-new`/`job` routes replaced by `tasks`/`task`; `sectionOf` now
  groups `run` under Pipelines (a run always belongs to a pipeline now, never a job).
- **`graph.ts`**: rewritten around `TaskRef` instead of an embedded `Task` — `newTask(kind)` becomes
  `newRef(taskId, existing)` (a reference to a task the caller already created/picked, not a fresh
  embedded config), `removeTask` → `removeRef`. Structural rules (`checkConnection`, cycle
  detection, `parseTaskField`, `hasOverlappingPositions`) are otherwise unchanged — they only ever
  cared about key/dependsOn/position, never about `kind` or embedded config.
- **`components/DagCanvas.tsx`**: node card shows the referenced task's resolved name (a new
  `taskNames` prop, since a `TaskRef` alone carries no display name) and its version badge
  (`latest` / `v3`), not code/runner/retry — those live on the separate Task now. `kind`'s
  three-way bar styling is gone; every node gets the same neutral bar.
- **`components/TaskConfigForm.tsx`** (new): the code/runner/retry/params/timeout/platformAccess
  editor, extracted from the old `TaskPanel` so it can be embedded both in the canvas's per-node
  panel and in the standalone Task library's detail page — the same config-editing UI, two call
  sites, since a task's own config is now edited independently of any pipeline that references it.
- **`components/ReferencePanel.tsx`** (new, replaces `TaskPanel`): configures one DAG node — which
  task it references (a picker: search existing, or "+ Create new task" inline), which version
  (latest or pinned), key, dependencies — plus embeds `TaskConfigForm` for the referenced task's
  current config, with its own "Save as new task version" action (independent of the pipeline's
  own save; auto-advances the node's pinned version to the one just saved, so an edit made from the
  canvas takes visible effect on that node immediately). "Remove from pipeline" drops the reference
  only, never the standalone Task.
- **`components/PipelineScheduleForm.tsx`** (new): the ADR 0065 scheduling UI relocated from the
  old `JobForm` onto Pipeline directly — schedule, role ceiling, concurrent-runs, and the new
  version-pin selector. No default-retry section: ADR 0071 leaves no pipeline-level retry fallback
  to configure (a task's own retry is the only retry policy there is, since phase 2).
- **`views/PipelineEditor.tsx`**: the canvas's "Add task" flow now creates a real standalone Task
  (`POST /tasks`) and references it, rather than filling in an embedded config; the "Start with
  source → transform → sink" starter is gone, replaced by a plain "+ Add task" in both the toolbar
  and the empty state. A "Builder" / "Schedule" tab switch was added — the schedule editor and the
  canvas share the page rather than living on separate routes.
- **`views/TaskViews.tsx`** (new): the Task library — `TaskList` (search, create, delete) and
  `TaskDetail` (name/description, version history, the same `TaskConfigForm`, "save new version").
- **`views/JobViews.tsx`**: deleted outright (Job is retired; nothing references it any more).
- **`views/PipelineList.tsx`**: gains Schedule/Next run columns and a "Run now" action directly
  (JobList's own capability, folded onto the pipeline it always described).
- **`views/RunView.tsx`**: "Back to job" → "Back to pipeline" (`run.pipelineId`, `run.jobId` no
  longer exists); the DAG view resolves each node's task name via a bulk `GET /tasks/{id}` fan-out
  (read-only, no config needed) instead of reading it off an embedded task.

## A real bug caught by manual browser verification, not by the test suite

`PipelineEditor`'s "+ Add task" generated the new node's key as `` `task_${refs.length + 1}` ``
rather than using `graph.ts`'s own collision-safe `uniqueKey` helper — harmless on an empty canvas,
but on `etl()`'s three-task fixture (`extract`/`clean`/`load`) it produced `task_4` where every
test expected `task_1`. The fake-backend RTL suite caught this immediately (a real, useful test
failure, not a fake-backend blind spot) once the assertions were written against the actual key —
fixed by routing `add()` through `uniqueKey("task", refs.map(r => r.key))`, the same function the
canvas's drag-and-drop path already used.

## Verified

- `tsc --noEmit`, `eslint src --max-warnings=0`, and `vite build` all clean.
- `vitest run`: 121 tests across 8 files, all passing — `graph.test.ts` (29, rewritten for
  `TaskRef`), `client.test.ts` (16), `navigation.test.ts` (15, rewritten for the `tasks` section),
  `PlatformAccess.test.tsx` (7, rewritten: platform-access toggling now saves as a task version, not
  a pipeline version; role-ceiling tests move to the Schedule tab), `PipelineApp.test.tsx` (36,
  substantially rewritten: task creation/picking/config-saving flows, the retired starter button,
  field-error routing against the new task-version-save endpoint, scheduling replacing the old
  "jobs" describe block), `RunView.test.tsx` (7), `ScheduleEditor.test.tsx` (8, unchanged —
  `ScheduleEditor` itself has no Job dependency), `ErrorBoundary.test.tsx` (3).
- **Manual verification in a real browser**, against the real backend (dev-memory store) behind a
  real OIDC identity provider (`hack/dev-keycloak.sh`, real Keycloak, real signed tokens) — not just
  the fake-backend unit suite: created a pipeline, added a task from the canvas (confirmed the real
  `POST /tasks` call and the resulting node), selected it and confirmed the real live-validation
  endpoint correctly refused to save with "tasks[0] references task '...', which has no saved
  version yet" (proving `_pin_task_versions`'s save-time check is wired end to end, not just
  covered by the fake backend), the full `TaskConfigForm` inline in the reference panel, the Task
  library's list and detail pages (including the just-created task, matching what the canvas
  created), and the pipeline's Schedule tab (version pin, role ceiling, concurrent-runs, all
  rendering with the correct copy). The code/storage pickers themselves could not be exercised
  fully end-to-end (no booth-catalog/booth-storage instance was running locally) — they showed the
  correct "could not be reached" fallback instead, which is itself the behavior under test for that
  path; the picker's actual selection flow is covered by the fake-backend suite instead.

## Not done (by design)

- YAML export. Phase 4.
