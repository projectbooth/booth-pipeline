# 0016: Adopting ADR 0074, phase 0 — the screens-first design pass for the `web/` rebuild

Status: **design pass done and ruled on (2026-09-25).** This document is the spec the rebuild is
built against. The user's rulings on the four open questions are in §10. Q2 changed the DAG tab
from a view/edit toggle to an always-editable canvas, and §4 reflects that.

Inputs: ADR 0074; `agent-briefs/pipeline.md` (the design-review section and everything after it);
the Figma reference at `examples/pipeline` (all nine `screens/` and the `PipelineView.tsx` /
`TaskView.tsx` source); the old `web/` (so nothing already validated gets dropped); and the backend
routes in `src/booth_pipeline/api.py`, which are **frozen**. Every screen below is backed only by
endpoints that already exist.

---

## 1. Hard constraints (from ADR 0074)

- **Mount contract unchanged.** Package `@projectbooth/pipeline-ui`, export `PipelineApp` with
  `{workspace, role, theme, getAccessToken}` plus the optional `basePath` / `onNavigate`. The
  `./dist/style.css` export stays. The root element has **no outer padding** (ADR 0072, since the
  shell owns it). booth-design needs only a pin bump.
- **Backend and API unchanged.** When the Figma shows something the API cannot supply, it is
  dropped or derived from existing data (§3). No new endpoints.
- **Functional parity** with everything the old UI got right (checklist in §7).

## 2. Global frame

```
┌ (shell chrome, shell padding) ───────────────────────────────────────────────┐
│ [Pipelines] [Tasks]                                   ← section tabs, no sidebar
│ ─────────────────────────────────────────────────────────────────────────────
│ ← Pipelines / Daily ETL                               ← breadcrumb (detail pages)
│ <page>
└──────────────────────────────────────────────────────────────────────────────┘
```

- **No sidebar.** The Figma's left rail (Pipelines / Tasks / Clusters / Settings) duplicates the
  shell's own navigation. Pipelines and Tasks stay as a top section switch. Clusters and Settings
  have no backend and are dropped.
- **Theme.** Both `dark` and `light` via `data-theme`, like today. The Figma is dark-only.
- **Palette.** Keep the fleet's Tailwind slate/indigo tokens (decision 0004: the four native
  modules look alike in the shell). Take the Figma's *structure* and its *status semantics*:
  green = succeeded, red = failed, blue = running/retrying, amber = skipped, grey = queued/pending,
  slate = canceled. **See open question Q3.**
- **Type.** Monospace for ids, keys, cron expressions, timestamps and durations, as in the Figma.
- **Timestamps.** One `formatTime` everywhere (absolute local time), with relative time
  ("2h ago") as a `title` tooltip.

### Routes

The tab is part of the URL, so every screen can be deep-linked and the live click-through can
visit each one directly. Old URLs keep working: `/pipelines/:id` lands on the DAG tab, and
`/pipelines/:id/v/:n` still opens an old version.

| Route (under `basePath`)            | Screen                                   |
|-------------------------------------|------------------------------------------|
| `/pipelines`                        | S1 Pipelines list                        |
| `/pipelines/:id`                    | S2a Pipeline, DAG tab                    |
| `/pipelines/:id/v/:n`               | S2a, showing an old immutable version    |
| `/pipelines/:id/runs`               | S2c Runs tab                             |
| `/pipelines/:id/schedule`           | S2d Schedule & Triggers tab              |
| `/pipelines/:id/config`             | S2e Configuration tab                    |
| `/pipelines/:id/versions`           | S2f Versions tab                         |
| `/runs/:id`                         | S3 Run detail                            |
| `/tasks`                            | S4 Tasks list                            |
| `/tasks/:id`                        | S5a Task, Code tab                       |
| `/tasks/:id/config`                 | S5b Task, Configuration tab              |
| `/tasks/:id/versions`               | S5c Task, Versions tab                   |

The task's Edit mode (S5d) is component state, not a route. It holds unsaved work, and a URL
that reopened an empty editor would be misleading.

---

## 3. What the Figma shows vs. what the API can back

| Figma element                                   | Source in the frozen API                                                                                                                                                                                               | Decision |
|-------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|---|
| Pipeline status badge (list + header)           | Most recent run's `status` (`GET /runs?pipelineId=&limit=1`)                                                                                                                                                           | Build ("Never run" when there are no runs) |
| Last run / Duration                             | Same run: `startedAt`, `finishedAt − startedAt`                                                                                                                                                                        | Build |
| Next run                                        | `Pipeline.nextRunAt`                                                                                                                                                                                                   | Build ("Manual only" when unscheduled or disabled) |
| Schedule / Trigger                              | `Pipeline.schedule`, cron or interval                                                                                                                                                                                  | Build, as a human description plus the raw cron |
| Owner                                           | `Pipeline.createdBy`                                                                                                                                                                                                   | Build, labelled "Owner" |
| Tasks count                                     | Node count of the current spec (draft, else latest version)                                                                                                                                                            | Build |
| Tags, Cluster, Pipeline timeout, Notifications  | Nothing                                                                                                                                                                                                                | **Drop.** The runner shows where Cluster was |
| Runs tab stats (success rate / total / failed)  | `GET /runs?pipelineId=&status=…&limit=1`, whose `.total` is an exact count per status                                                                                                                                  | Build, with exact all-time counts, not a sample |
| Runs history strip                              | Last ≤ 30 runs from the runs page                                                                                                                                                                                      | Build |
| DAG node status borders                         | `GET /runs/:id` → `tasks[].status`, keyed by `taskKey`                                                                                                                                                                 | Build, from the latest run (§5) |
| Node type badge (NOTEBOOK / SQL / …)            | Resolved task config: `code.language` (`python` / `sql`) and `runner`                                                                                                                                                  | Build as a language badge. There is no `kind` (ADR 0071) |
| Task list Status / Duration / Last run          | No per-task run endpoint. `TaskRun` is keyed by the pipeline's node key, not the task id, and no endpoint says which pipelines reference a task                                                                        | **Drop** (Q1) |
| Task Runs tab                                   | Same gap                                                                                                                                                                                                               | **Replace with a Versions tab** (Q1) |
| "Run Task" button                               | No single-task run endpoint                                                                                                                                                                                            | **Drop** (Q1) |
| "Edit" button                                   | Decorative in the Figma                                                                                                                                                                                                | Pipelines: none, the canvas is always editable (Q2). Tasks: a real edit mode (S5d) |

---

## 4. Screens

Every screen defines five states: **loading** (skeleton, same layout, no layout shift),
**error** (inline banner with Retry, other tabs still usable), **empty**, **read-only**
(`role === "viewer"`: all write controls hidden, never merely disabled) and **ready**.

### S1 — Pipelines list  (Figma screen 1)

```
Pipelines                                                        [+ New pipeline]
Orchestrate tasks, schedule runs, and track their history.

[🔍 Search pipelines…          ]  ( All 12 | Running 1 | Failed 2 | Succeeded 8 | Never run 1 )

NAME                    STATUS      TRIGGER           LAST RUN          DURATION  NEXT RUN          OWNER
Daily ETL               ● Running   ⏱ 0 6 * * *       2026-09-24 06:00  —         2026-09-25 06:00  alice      [▶ Run] [⋯]
  End-to-end ingestion…            daily at 06:00 UTC
Churn retrain           ● Failed    ↻ every 15 min    …                                                     [▶ Run] [⋯]
Ad-hoc export           ○ Never run  Manual only       —                 —         —                 bob        [▶ Run] [⋯]
```

- Data: `listPipelines(q)`, then the latest run for each pipeline. One `GET /runs?limit=200`
  covers most pipelines. Any pipeline missing from it gets its own `?pipelineId=&limit=1` call
  (bounded concurrency of 6), so the status column is never wrong just because a pipeline ran long ago.
- The status filter counts are derived from those latest runs. Search is server-side (`q`),
  debounced by 250 ms.
- Clicking a row opens S2a. **▶ Run** (writers only) calls `runPipeline` and navigates to S3.
  If the pipeline has no saved version, the button is disabled with the tooltip
  "Save a version first — Run now runs the latest saved version".
- `⋯` → **Delete pipeline…** opens an inline confirm row (never `window.confirm`), then `DELETE`.
  The backend cancels active runs, and the confirm text says so.
- **+ New pipeline** expands an inline card above the table (Name*, Description,
  [Create] [Cancel]) → `createPipeline` → S2a (an empty canvas, ready to add tasks).
- Empty: "No pipelines yet", with a Create button for writers or "Someone with edit access needs
  to create one" for viewers. With no search matches: "No pipelines match 'x'".
- Auto-refresh every 10 s while any visible pipeline's latest run is queued or running.

### S2 — Pipeline detail: shared header  (Figma screen 3, top)

```
← Pipelines / Daily ETL
Daily ETL  ● Running   Draft ahead of v4                                  [▶ Run now]  [⋯]
End-to-end ingestion from raw S3 events to warehouse-ready Gold tables.
Run now runs v4 (latest saved version)          ← one line of small print under the button row
┌──────────────┬──────────┬──────────────────┬──────────────────┬────────┬───────┐
│ LAST RUN     │ DURATION │ NEXT RUN         │ SCHEDULE         │ OWNER  │ TASKS │
│ 2026-09-24…  │ 7m 20s   │ 2026-09-25 06:00 │ 0 6 * * * (UTC)  │ alice  │ 6     │
└──────────────┴──────────┴──────────────────┴──────────────────┴────────┴───────┘
  DAG    Runs    Schedule & Triggers    Configuration    Versions
─────────────────────────────────────────────────────────────────
```

- **Stat strip** (new, from the Figma): six cards, 6 × 1 at ≥ 1024 px, 3 × 2 at ≥ 640 px,
  2 × 3 below that. Last run links to S3. Schedule shows "Manual only" or "Paused" when unscheduled
  or disabled.
- **▶ Run now** (new here, and the headline fix) sits on the header, so it is reachable from
  **every tab**. It calls `runPipeline` and navigates to S3. Because the backend runs
  `pinnedVersion ?? latest saved version` and never the draft (`service.start_run`), the small
  print under the button always names the version: "Runs v4 (latest saved)" or "Runs v2 (pinned)".
  When the draft differs from the version that will run, it adds a warning: "Your draft changes
  aren't in a saved version, so this run won't include them." If there is no saved version,
  the button is disabled and says why. A 409 (already running, concurrency off) shows as an inline
  banner with a link to the active run.
  With unsaved canvas changes, Run now stays enabled (it runs a saved version regardless), and
  the small print makes that explicit.
- **⋯** → Export YAML (the version shown), Edit details (name/description via
  `updatePipeline`, which the API has but the old UI never offered), Delete pipeline.
- **Status badge** = latest run status. **Draft chip** appears only when the draft's spec
  actually differs from the latest version's spec, not merely because a draft row exists.

### S2a — DAG tab: the builder, always editable for writers  (Figma screen 3 canvas + old builder)

Ruled Q2: there is no view/edit toggle. Writers see the toolbar and an editable canvas the moment
the tab opens. Viewers see the same canvas, read-only, with no toolbar.

```
[+ Add task] [Tidy layout]  ● Unsaved changes      Version ▾(Current draft | v4 · 2026-09-24 14:02 · 6 tasks | …)  [Discard] [Save] [Save as new version…]
⚠ Not ready to save: tasks[1] references task 'x', which has no saved version yet      ← live validation
Status from run 7f3a… (v4, 2h ago)                                                     ← status caption
┌───────────────────────────────────────────────────────────────────────────────┐
│ ·  ·  ·  ·  (dot grid)                                                        │
│   ┌─PY──────────────┐        ┌─PY──────────────┐        ┌─SQL─────────────┐   │  560 px, fixed
│   │ ingest_raw      │──────▶ │ transform       │──────▶ │ aggregate       │   │
│   │ extract · latest│        │ spark_xf · v3   │        │ agg · v1   ●    │   │
│   └─────────────────┘        └─────────────────┘        └─────────────────┘   │
│   green border               green                      blue (running)        │
└───────────────────────────────────────────────────────────────────────────────┘
┌ Panel (below the canvas, in page flow; exactly one of these) ─────────────────┐
│ (a) nothing selected:  hint text                                               │
│ (b) Add a task:        TaskPicker                                              │
│ (c) node selected:     Last-run strip + Reference settings + the task's settings│
└───────────────────────────────────────────────────────────────────────────────┘
```

- **Layout (the canvas-collapse fix, §6):** the canvas and the panel are stacked siblings in
  normal block flow at every width. Nothing opens beside the canvas, over it, or inside a flex
  row with it, so opening or closing a panel cannot change the canvas height. This is exactly
  "all of the menu popups should be under the pipeline DAG view".
- **What it shows:** the **current spec**, meaning the draft if there is one, else the latest
  version. At `/v/:n` it shows version n with a banner: "Viewing v2 of 5 (created …). Saving
  creates v6 from this one; nothing pinned changes." (Same as before.)
- **Nodes** show a language badge, the **task name** (resolved in bulk on load, which also fixes
  the old "(no task picked) until clicked" gap), key · version reference, and a status dot.
- **Status borders** (new): each node's border takes the latest run's status for the node key
  (§5), polling every 2 s while that run is active. They stay on while editing. A node that is
  new or re-pointed since that run has no status, so it stays neutral. The red validation outline
  and the indigo selection ring take precedence.
- **Editing (writers):** drag nodes; drag from handle to handle to add a dependency; Backspace
  deletes the selected node; pan/zoom/fit controls. Viewers get pan/zoom only.
- **(b) Add a task** uses the same picker as before: search existing tasks; tasks with no saved
  version and no draft are hidden by default behind a "show unconfigured" toggle and badged
  "not configured yet"; typing a new name offers "+ Create task 'x'". Picking an existing task
  **never** creates a Task. Picking wires a node, selects it, and scrolls the panel into view.
- **(c) Node selected**, three sections:
  0. *Last run* (from the Figma's side panel): the status, attempts, duration and a "Logs" link to
     S3 filtered to this node, when the latest run has it.
  1. *Reference*: node key (rename, collision-checked), References task [Change → picker]
     [Open task →], Version: "Always follow latest (draft if saved)" / "Pin to v3 —
     2026-09-24 14:02", Depends on (multi-select), [Remove from pipeline]. For viewers these
     are the same rows, read-only.
  2. *Task settings* (collapsible, starts collapsed when pinned): the shared task's config form
     (code source picker: catalog / storage, runner, retry, timeout, platform access, params),
     with its own **[Save]** (task draft) and **[Save as new task version…]**, plus a notice
     that this edits the shared task and affects every pipeline following "latest". When the
     node is pinned, the form is read-only and explains why: "Pinned to v2 — pinned versions
     are frozen. Switch to 'latest' or open the task to edit it."
- **Save** → `PUT /pipelines/:id/draft`. No version, no prompt. Banner: "Saved. Runs still use
  v4 until you save a new version." (This copy matters: pipeline-level Run now uses a saved
  version, even though task "latest" refs pick up task drafts.)
- **Save as new version…** → a small inline popover **below the toolbar** (not a modal over the
  canvas) with an optional "What changed?" note → `POST …/versions`. Banner: "Saved as v5."
- **Discard** reloads from the server after an inline confirm. Switching tabs or navigating
  away with unsaved changes shows an inline "Discard unsaved changes?" bar; a `beforeunload`
  guard covers reloads. (Tab switches keep the canvas state in memory, so switching to Runs and
  back loses nothing. The bar only appears when leaving the pipeline.)
- **Live validation** calls `POST /pipelines/validate` debounced at 500 ms. The server is the
  only rule source. A field error (`tasks[N].x`) outlines node N in red, selects it, and routes
  the message to input `x` in panel (c). This keeps the old field-routing fix.
- **Empty spec:** a centered card inside the canvas, "This pipeline has no tasks yet", with
  [+ Add task] for writers.

### S2c — Runs tab  (new, Figma screen 4)

```
┌ SUCCESS RATE ┐ ┌ TOTAL RUNS ┐ ┌ FAILED ┐ ┌ HISTORY (last 30, oldest → newest) ──────────┐
│ 71%          │ │ 7          │ │ 1      │ │ ▇▇▇▇▇▇▇▇▇ ▇▇▇▇▇▇▇▇ ▇▇▇▇▇ … each bar is a link  │
└──────────────┘ └────────────┘ └────────┘ └──────────────────────────────────────────────┘
RUN      STATUS       VERSION  STARTED            DURATION  TRIGGERED BY
7f3a…    ● Running    v4       2026-09-24 06:00   —         Scheduled
1c9e…    ● Succeeded  v4       2026-09-23 06:00   7m 20s    alice (manual)
```

- Totals come from exact server counts (`status=` filter + `.total`). Success rate is
  succeeded ÷ (succeeded + failed); canceled runs are excluded, and the tooltip says so.
- Bars are colored by status with a tooltip (id, status, started, duration) and link to S3. Bar
  height is uniform, not duration-scaled, which is honest and simple.
- The table shows 50 rows with [Load more] (`offset`). Rows link to S3. Polls every 3 s while any
  row is active. Empty: "No runs yet. [▶ Run now]".

### S2d — Schedule & Triggers tab  (Figma screen 5)

```
┌ Trigger ──────────────────────────────────────────── [Edit schedule] ┐
│ Trigger type        Cron  (or Interval / None — manual only)          │
│ Expression          0 6 * * *   "daily at 06:00"                      │
│ Timezone            UTC                                                │
│ Enabled             Yes                                                │
│ Next run            2026-09-25 06:00 UTC                               │
│ Runs version        Always the latest saved version  | Pinned to v2 — 2026-09-20 11:03
│ Concurrent runs     Not allowed                                        │
│ Platform access cap Viewer — read only                                 │
└───────────────────────────────────────────────────────────────────────┘
ℹ Scheduled: runs daily at 06:00 UTC. Next: 2026-09-25 06:00.   |   Manual only: use ▶ Run now above.
⚠ Scheduled before workload identity: unattended runs get no platform access until this schedule is saved again.  (when !hasOwner)
```

- The read-only summary is the default view, as in the Figma. **Edit schedule** swaps the card
  in place for the editor: frequency presets (every N seconds / minutes / hours, daily, weekly,
  custom cron), timezone, enabled, allow concurrent runs, role ceiling, and the **version pin
  picker with creation timestamps** ("Always the latest saved version" / "Pin to v3 — <time>").
  Buttons: [Save schedule] [Cancel] → `PUT …/schedule`.
- The Notifications and Pipeline timeout sections are dropped (§3).
- The "Manual only" banner points at the Run now button, which now exists on this screen.

### S2e — Configuration tab  (new, Figma screen 6)

Flat, read-only key/value cards. No inputs.

```
General           Pipeline ID (copy) · Name · Description · Owner · Created · Updated
Versions          Latest saved version v4 (2026-09-24 14:02) · Draft: differs, saved 10 min ago by alice · Runs use: latest | pinned v2
Execution         Allow concurrent runs · Platform access cap
Tasks (current)   KEY          TASK (link)       VERSION REF     DEPENDS ON
                  ingest_raw   extract           latest → draft  —
                  transform    spark_xf          v3              ingest_raw
```

### S2f — Versions tab  (parity: per-entity history, reached from the entity)

```
VERSION  CREATED            BY     TASKS  NOTES                      
v4 latest 2026-09-24 14:02  alice  6      add quality checks   [Open] [Export YAML]
v3        2026-09-22 09:10  bob    5      —                    [Open] [Export YAML]
Draft     saved 10 min ago  alice  6      differs from v4      [Open]
```

- **Open** goes to `/pipelines/:id/v/:n` (S2a for that version). **Export YAML** downloads
  `name-vN.yaml`. The draft row appears only when the draft differs from the latest version.

### S3 — Run detail  (old RunView, restyled; not in the Figma)

```
← Pipelines / Daily ETL / Run 7f3a1c2e
Run 7f3a1c2e  ● Running                                                       [Cancel run]
┌ STATUS ┬ STARTED ┬ DURATION (live) ┬ VERSION v4 (link) ┬ TRIGGERED BY ┬ TASKS 3/6 done ┐
[ DAG, 420 px fixed, read-only, status borders from this run, click a node to filter logs ]
TASK         TASK NAME   STATUS       ATTEMPTS  STARTED  DURATION  ERROR
ingest_raw   extract     ● Succeeded  1         …        1m 42s    —          [Logs]
Logs — whole run ▾ (or: ingest_raw)                                   [Show the whole run]
┌ monospace log stream, stdout/stderr/system tinted, follows tail while active ┐
```

- Polls every 1.5 s while queued or running and stops once the run is terminal. Cancel shows
  "Cancelling…" once `cancelRequested` is set.

### S4 — Tasks list  (Figma screen 2)

```
Tasks                                                                      [+ New task]
Reusable, versioned units of code. Pipelines reference them.
[🔍 Search tasks…]
NAME (mono)        DESCRIPTION                 VERSION                  UPDATED           CREATED BY
extract            Pull raw events             v3                       2026-09-24 14:02  alice   [⋯]
sketch             —                           Draft only               …                 bob     [⋯]
task_7             —                           not configured yet       …                 bob     [⋯]
```

- One row per task entity (the dedup, as before). Version history is not in the list: it lives
  on S5c.
- *Built in phase 1:* the version badge is `vN` / "Draft only" / "Not configured yet". Whether a
  draft actually *differs* from vN can't be told from the entity alone (it needs both configs), so
  that finer "draft ahead" state is shown on the task page (S5), not in the list.
- The Figma's Type / Status / Duration / Last run / Cluster columns are dropped (§3, Q1). Type is
  shown on S5 instead, where the config is loaded anyway.
- **+ New task** opens an inline card (Name*, Description) → `createTask` with no config →
  S5d edit mode. `⋯` → Delete, with an inline confirm. The backend's in-use refusal ("still
  referenced by a pipeline version") is shown as-is. It doesn't name the pipeline, and adding that
  would need backend work. Its known blind spot (it doesn't see references held only in a
  pipeline *draft*, per the brief) is also backend-side and out of scope here.

### S5 — Task detail: shared header  (Figma screens 7–9, top)

```
← Tasks / extract
extract  PYTHON  ● v3 · draft ahead                                              [Edit]  [⋯]
Pull raw events from the landing zone.
(Runner base) (Retries 2 · exponential) (Timeout 30m) (Platform access: on) (Created by alice)   ← meta chips
  Code    Configuration    Versions
Showing: Current draft ▾ (Current draft | v3 · 2026-09-24 14:02 | v2 · …)       ← on Code + Configuration
```

- Meta chips follow the Figma's chip row, not a stat grid. Tasks have no run stats (§3).
- **⋯** → Edit details (name/description), Delete task.
- No "Run Task" button (§3, Q1).

**S5a Code** (Figma 7): source line (`catalog: etl/raw_ingest @ 1.4.0` / `storage: s3-main:
jobs/x.py` / `inline`), language, [Copy]; below that, the snapshotted `code.source` in a
line-numbered monospace block. If no source was snapshotted: "Source not captured for this
version."

**S5b Configuration** (Figma 9): flat cards. *Task settings*: Task ID, Name, Language, Runner,
Code reference, Retries (count, delay, backoff), Timeout, Platform access. *Parameters*: key → value
rows in mono.

**S5c Versions**: version, created, by, notes, [View] (sets the "Showing" selector). The draft
row appears when the draft differs.

**S5d Edit mode**: the full config form (the same component as panel (c) in S2a), with
[Discard] [Save] [Save as new version…]. It starts from the draft if there is one, else the latest
version, else defaults. Copy: "Save updates the draft that every pipeline following 'latest'
uses immediately; pinned pipelines are unaffected."

### Dialogs and overlays

The module has no modal dialogs at all. Confirmations are inline rows, and the version-note
prompt is an inline popover below the toolbar. Nothing ever draws over the canvas, and nothing
depends on the shell's z-index stack.

---

## 5. DAG status coloring rules

- Source: the most recent run of the pipeline (`GET /runs?pipelineId=&limit=1`, then
  `GET /runs/:id`). Match by **node key**. If the run was of a different version, keys that no
  longer exist are ignored and new keys stay neutral. The caption names the run's version so a
  mismatch is visible.
- Borders stay on while editing. A node whose key or task reference changed since that run is
  neutral, because the run says nothing about it. The red validation-error outline and the
  indigo selection ring take precedence.
- Mapping: succeeded → green, failed → red, running/retrying → blue with a pulsing dot,
  pending/queued → grey dashed, skipped → amber, canceled → slate.

## 6. Canvas layout rules (the collapse, designed out rather than patched)

The old bugs all came from the canvas's height being *derived*: a `flex-1` item in a container
that switches between row and column at `lg`, sharing an axis with an `<aside>`, plus an `h-full`
percentage chain down into React Flow. The rebuild removes derivation entirely:

1. The canvas wrapper is a plain block element with an **explicit pixel height** (560 px on the
   DAG tab, 420 px on S3, 480 px below 640 px wide). It is never a flex or grid item whose size
   is computed from siblings.
2. React Flow's own root gets `width: 100%; height: 100%` against that definite pixel height.
   That is one resolution step from a fixed number, with nothing in between.
3. Every panel (node detail, add-task picker, reference/task settings, validation banner,
   version-note popover) is a **sibling rendered after** the canvas wrapper in normal flow. The
   page grows downward; the canvas never shares an axis with anything.
4. `fitView` runs once when nodes first become known, not on every render.
5. The same layout applies at every width. There is no breakpoint-dependent arrangement of
   canvas vs. panel, so there's no `lg` threshold to get wrong.

The click-through for this is in §8. It uses a **real** viewport resize via Playwright's
`setViewportSize`, because Chrome-extension resizing silently no-op'd in three previous sessions.

## 7. Parity checklist

| Must keep (ADR 0074)                                                               | Where |
|------------------------------------------------------------------------------------|---|
| Task and Pipeline lists dedupe by entity; version history on the entity with creation times | S1, S4, S2f, S5c |
| Every version picker shows the version's creation time                              | S2a version selector and version pin, S2d pin picker, S5 "Showing" selector |
| Add-task picker distinguishes configured from unconfigured and never creates orphans | S2a (b) |
| Plain Save = draft; Save as new version = immutable; "latest" resolves the draft; pins frozen | S2a, S5d; pinned refs lock the inline task form |
| Run now from the pipeline's own page                                                | S2 header, on every tab |
| YAML export                                                                         | S2 ⋯ menu, S2f per-version |
| Scheduling (cron/interval/presets/timezone/enabled/concurrency/role ceiling) plus `pinnedVersion` | S2d |
| Live server validation with field-level routing to the right node and input         | S2a |
| Read-only role hides every write control                                            | all screens |
| Error boundary per route; catalog/storage outages don't break the form              | global; S2a (c), S5d |
| Per-run DAG, task table, logs, cancel                                               | S3 |
| Unsaved-work guard                                                                  | S2a, S5d |
| New from the Figma: stat header, Runs tab with pass/fail strip, status-colored nodes, flat Configuration tab | S2, S2c, S2a/§5, S2e and S5b |

## 8. Implementation phases (each ends with a check-in)

1. **Foundation**: fresh `web/src` on branch `rebuild/adr-0074` (old code stays in git history),
   same `package.json` name/exports/build, `PipelineApp` props identical (type-checked against a
   copy of the old `PipelineAppProps`); typed API client covering every route incl. `status` /
   `offset`; routing with tab paths; UI primitives (Button, Badge, StatCard, KV card, Tabs,
   Banner, InlineConfirm, Table, Skeleton) in both themes; S1 and S4 including create and delete.
2. **Pipeline read side**: S2 header, stat strip and Run now; S2a with bulk name resolution and
   status borders; S2c, S2e, S2f; S3.
3. **Pipeline editing**: S2a editing (canvas rules §6, picker, reference and task panels, draft vs.
   version, validation routing, discard guard); S2d editor; export.
4. **Task detail**: S5a–S5d.
5. **Verification and release**: RTL tests per screen plus a mount-contract test; then a **live
   click-through of every screen and every state in this document** against a real backend and
   real Keycloak tokens, at **1440 px and 900 px** real viewports, recording canvas height
   before, during and after selecting a node, opening Add task, and opening Save as new version.
   Then release (Q4) and hand the pin bump to booth-design.

   **Measurement pitfall found in phase 2:** a backgrounded browser tab (as the Chrome-extension
   automation tab often is: `document.visibilityState === "hidden"`) never fires
   `requestAnimationFrame`. React Flow's fit and layout steps wait on a frame, so any canvas
   measurement taken in a hidden tab reads a half-initialised canvas: identity transform, or a fit
   to a stale size. It is plausibly why earlier sessions measured canvas collapses that other
   sessions couldn't reproduce. Phase 5 therefore measures with Playwright (frames always render)
   at real viewport sizes, and asserts `visibilityState === "visible"` before trusting a number.

## 9. Decisions taken in this pass (reversible, flagged for visibility)

- **The pipeline canvas is always editable** for writers (ruled Q2; the Figma's Edit button is
  dropped for pipelines). The task detail page keeps a separate Edit mode (S5d), because the
  flat read-only Configuration tab there is itself one of the Figma items ADR 0074 asked for.
- **Panels go below the canvas, not to its right** as in the Figma, per the user's direct
  instruction in the brief. This is also the core of the layout fix (§6).
- **A fifth "Versions" tab** on pipelines, and Versions in place of Runs on tasks, so version
  history stays reachable from the entity without cluttering Configuration.
- **"Edit details" (rename/description)** is added for pipelines. The API always had it; the old
  UI never exposed it.
- **Run now names its version**, because it never runs the pipeline draft (`service.start_run`).
  Without that, plain Save plus Run now would silently run stale DAG wiring.

## 10. Rulings (user, 2026-09-25)

- **Q1: task run history and "Run Task": dropped.** The Figma's task Runs tab, its task-list
  Status/Duration/Last-run columns, and "Run Task" need backend support that doesn't exist (a
  per-task run query, a single-task run). The Versions tab takes the Runs tab's place.
- **Q2: always-editable canvas.** No view/edit toggle on the pipeline DAG tab (S2a).
- **Q3: keep the fleet palette.** Shared slate/indigo tokens, so the module matches catalog and
  storage in the shell. The Figma's structure and status colors are adopted; its palette is not.
- **Q4: release as `1.0.0`.** The props contract is unchanged; the major bump marks the rewrite.
