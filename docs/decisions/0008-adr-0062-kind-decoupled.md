# 0008: Adopting ADR 0062 — `kind` is optional and purely decorative

Status: **built.**

## What changed

- `model.py`: `Task.kind` went from a required `Literal["source","transform","sink"]` to `Kind | None
  = None`. `validate_structure` no longer enforces "source has no deps," "transform/sink needs at
  least one," or "sink has no downstream" — the only rules left are the DAG's actual mechanics
  (duplicate keys, self-loops, duplicate edges, unresolved dependencies, cycles, runner
  availability). A task with no `kind` set is fully valid.
- `web/src/graph.ts`: `checkConnection` drops the same two kind-gated refusals; `newTask()` no
  longer requires a `kind` argument (`newTask(existing, kind?)` — the hint is now optional and
  used only to pick a nicer starter key/template).
- `web/src/views/PipelineEditor.tsx`: the three `+ Source` / `+ Transform` / `+ Sink` buttons
  collapse into one `+ Add task`. The empty-canvas starter template (a suggested source → transform
  → sink chain) is kept as a convenience, not a requirement — its copy no longer implies it's the
  only valid shape.
- `web/src/components/TaskPanel.tsx`: the "Type" field is no longer `required`; it gained an
  "— untagged —" option, and no longer clears `dependsOn` when switched to "source" (that whole
  behavior existed only to enforce the rule this ADR removes).
- `web/src/components/DagCanvas.tsx`: every task renders both handles regardless of kind (or lack
  of one); an untagged task gets a neutral grey bar (`UNTAGGED_STYLE`) instead of falling into one
  of the three kind colors.
- Two small, no-ADR items bundled in: the Pipeline/Job list subtitles now state the relationship
  between them explicitly (not just describe each in isolation), and `test_platform_clients.py`'s
  X-Booth-Workspace assertion (pre-existing, unrelated) was left untouched.

## Verified

- `test_model.py`: every one of the four removed rules is now asserted **valid**
  (`test_kind_is_optional_and_purely_decorative_never_a_dependency_constraint`), plus round-tripping
  `kind: null` and confirming an old, stricter-tagged pipeline stays valid unchanged.
- `graph.test.ts`: the same four cases at the frontend mirror, plus untagged-to-untagged connecting
  freely.
- `PipelineApp.test.tsx`: the "Type" select is asserted not required, switching it (including to
  untagged) leaves the DAG's wiring untouched, and the "+ Add task" flow is exercised end to end
  (including the pre-existing validation-error and viewer-read-only paths, updated for the new
  button/testid).
- Full suites green: 260 Python (`pytest tests/unit tests/contract`), 104 web (`vitest`), `tsc -b`,
  `eslint .`, `ruff check`, `npm run build`.

## Not done

- No soft "this task looks disconnected" warning was added — ADR 0062 explicitly leaves that as an
  optional follow-up, not required scope, and none was requested.
- `booth-e2e`'s existing `kind: "source"` workaround (for a standalone task) is unaffected either
  way and needs no change on this side; per the ADR it could now drop `kind` from its payload
  entirely, which is a note for its own brief, not something to change here.
