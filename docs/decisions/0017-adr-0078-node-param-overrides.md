# 0017: Adopting ADR 0078 — a DAG node's own param overrides

Status: **built.** ADR 0078 fixed the requirement (one pipeline, several nodes referencing the same
task, each with its own value — the motivating case is four per-minute stock-quote cron jobs
becoming one pipeline of four `fetch_quote` nodes) and four hard constraints, and left the
mechanism to this module. This records the mechanism chosen and why.

## The gap, confirmed in code first

`TaskConfig.params` is a fixed dict on a task's own versioned config, and the engine passed it
straight through (`engine._make_op`: `params=config.params`); `service.start_run` takes no params
at all. So running the same code with a different value meant a whole separate Task entity per
value — duplicating code reference, runner, retry and timeout for what is one script.

## Schema: `TaskRef.paramOverrides`

```jsonc
{ "key": "hood", "taskId": "…", "taskVersion": 3, "dependsOn": [],
  "position": { "x": 0, "y": 0 },
  "paramOverrides": { "symbol": "HOOD" } }      // new, optional, default {}
```

- **On the reference, inside the spec** (constraint 1). A pipeline draft and every immutable
  version carry each node's overrides exactly as they carry its task reference, version pin and
  wiring: fixed at save time, versioned, reproducible. No side table, no migration: specs are
  stored as their JSON dump and read back through `PipelineSpec.model_validate`, so a spec saved
  before this change loads with `paramOverrides: {}` and behaves exactly as before.
- **Named `paramOverrides`, not `params`.** A task already has `params` (its defaults); a second
  field with the same name on the node would blur the two in the API, the YAML export and the UI.
- **Validated by the same rule as a task's `params`** (constraint 3): one shared
  `model.check_params` — JSON-serialisable, at most `MAX_PARAMS_BYTES`. The merged result is held
  to the same cap at resolution (below). No new type system, no templating.

## Merge: shallow, key by key, at resolution — `resolve.node_config`

`effective = {**task.params, **node.paramOverrides}`.

- **Where**: `resolve.resolve_task_configs`, the TaskRef→TaskConfig step ADR 0071's phase-2
  correction introduced. It already returned one config **per node key**, not per task, so two
  nodes referencing the same task naturally get two different configs — and every path that runs
  or checks a spec goes through it: run start (`runs.py`), live validation and save-time
  compile-check (`service.py`). The engine is unchanged: it still reads `config.params` per node.
- **Shallow** because it is predictable at a glance: a key set on the node replaces the task's
  value for that key, whole (a nested object is replaced, not deep-merged); keys the node doesn't
  set keep the task's value. That covers the motivating case (override `symbol`, keep `exchange`)
  without inventing semantics for merging nested structures or deleting keys.
- **Never mutates the task** (constraint 4): the merge produces a copy
  (`model_copy(update=...)`); the resolved task config — possibly shared by other nodes — is
  untouched, and a node with no overrides gets that config object itself. The task's own
  `params` stay its defaults: for every node that doesn't override, for other pipelines, and for
  the task on its own.
- **Size**: if the merge exceeds `MAX_PARAMS_BYTES`, resolution raises a `ModelError` on
  `tasks[N].paramOverrides`, so live validation and save both refuse it, naming the node.

Out of scope, per the ADR: values supplied at trigger time. Nothing here reads a value from a run
request or a schedule; a different value means saving the pipeline.

## Export (constraint 2)

Each exported node carries `paramOverrides` beside `task.config`, which stays the task's own,
un-merged config — so the document states both the default and the node's change, and the value
a node runs with is derivable exactly as the engine derives it.

## UI

- **Node editor** (below the canvas): a "Parameters for this node" card between the node's
  wiring and the task's own settings — the task's params (read-only, its defaults), this node's
  overrides (a JSON object, the same authoring style as a task's params), and **Runs with**: the
  merged result, with this node's keys marked. The preview uses the same shallow merge as the
  server. Anything but a JSON object is refused inline without touching the node. Server errors
  on `tasks[N].paramOverrides` land on this field. Overrides are editable even when the node is
  pinned to a task version: they belong to the node, not to the (frozen) task version.
- **Canvas**: a node with overrides shows the first one on its card (`symbol=HOOD`, `+N` for
  more) — four `fetch_quote` nodes are otherwise indistinguishable.
- **Duplicate node**: copies a node (task, version, dependencies, overrides) under a new key, so
  the four-symbol pipeline is one "+ Add task" and three duplicates, each with one value changed.
- Read-only views (viewer's node details, the Configuration tab) show each node's overrides and,
  in node details, the params it runs with.

## Tests

- `tests/unit/test_param_overrides.py`, through the real API, resolution and subprocess runs:
  two nodes referencing one task with different overrides, plus one with none — each runs with
  its own value, not the task's or the other node's; downstream, each node's *output* differs;
  only one Task entity exists; the task's saved params are unchanged; overrides round-trip in
  drafts and versions; a pinned older version runs with its own older override; export; the size
  cap on an override and on the merge; a pre-ADR-0078 spec loads with none; the merge is shallow
  and never mutates the shared config.
- `web/src/__tests__/ParamOverrides.test.tsx`: setting, previewing, labelling and saving
  overrides; refusing non-objects; Duplicate; error routing; the viewer's read-only view.
