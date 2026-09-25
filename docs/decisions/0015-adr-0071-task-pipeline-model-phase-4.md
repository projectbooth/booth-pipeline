# 0015: Adopting ADR 0071, phase 4 — YAML export

Status: **phase 4 of 4 built**. This is the last phase of ADR 0071's adoption — Job's retirement
and Task's promotion to a standalone, versioned, reusable resource is now fully built out: data
model + migration (phase 1), API + engine (phase 2), builder UI + Task library (phase 3), and now
export.

## What changed

- **`export.py`** (new): `export_pipeline_version(store, workspace, pipeline, version) -> str`
  renders one immutable pipeline version as a YAML document. Export-only, per the brief, but a
  faithful, complete structure rather than a debug dump — each `TaskRef` is resolved to its
  referenced task's own `id`/`name`/`description`, its pinned version number, and that version's
  full `config` (code/runner/retry/params/timeout/platformAccess), not left as a bare
  workspace-private id. The pipeline's own `schedule`/`allowConcurrentRuns`/`roleCeiling`/
  `pinnedVersion` are included alongside the DAG. Shaped as the brief asked — a future import could
  read this same structure back, matching tasks by name/config content or id and recreating the
  DAG — without redesigning anything here first.

  A referenced task/version failing to resolve raises `ModelError` rather than silently omitting
  it from the export; this is a defensive path only; it should not be reachable in practice, since
  `delete_task` already refuses to remove a task still referenced by *any* saved pipeline version
  (past or present, not just the latest) — a guarantee that predates this phase (phase 1's store
  layer) and this phase leans on rather than re-deriving.

- **`service.py`**: `export_version(ident, pipeline_id, version) -> str`, mirroring `get_version`'s
  own shape (`version=None` means latest).
- **`api.py`**: `GET /pipelines/{pipeline_id}/versions/{version}/export`, returning the YAML text
  directly with `Content-Type: application/yaml` (not wrapped in the usual JSON envelope — this
  endpoint's whole point is to hand back something a person downloads and reads, or a future
  importer parses, not something this API's own JSON clients consume). Factored the existing
  `"latest" | int` version-parsing duplicated across `get_version`/`get_task_version` into one
  `_version_or_404` helper shared by all three routes now, rather than copying it a third time.
- **`pyproject.toml`**: `pyyaml` moves from `dev` to the real runtime dependencies — it was already
  present for test tooling, but production code now uses it for real.
- **`api/client.ts`**: `exportVersion`, using a new `requestText` (the existing `request` helper
  always parses JSON; this endpoint's body is YAML text).
- **`views/PipelineEditor.tsx`**: an "Export as YAML" button next to the version selector, enabled
  whenever a version is loaded (including for a read-only viewer — export is a read action, not
  gated behind write access). Downloads via the standard `Blob` + `URL.createObjectURL` + a
  synthetic anchor click pattern, named `<pipeline-name>-v<version>.yaml`.

## Verified

- `export_pipeline_version` checked directly against `MemoryStore` for the exact YAML shape (see
  the phase's own working notes) before wiring it into the service/API layer.
- Three new API-level tests (`test_export_is_a_faithful_yaml_snapshot_of_one_version`,
  `test_export_an_old_version_is_immutable_even_after_a_newer_save`,
  `test_export_of_an_unknown_version_is_404`) — the first parses the real YAML response with
  `yaml.safe_load` and asserts on structure (task wiring, dependsOn, the referenced task's own
  resolved config, the pipeline's schedule), not just that a 200 came back. Full backend suite
  (`pytest tests/unit`, real Postgres) still green.
- New frontend test (`exports the current version as a downloadable YAML file`) mocks
  `URL.createObjectURL`/`revokeObjectURL` and `HTMLAnchorElement.prototype.click`, and asserts the
  real export GET request fired, the resulting `Blob`'s MIME type is `application/yaml`, and the
  object URL was revoked after the click — the full download mechanics, not just that the button
  exists. Full frontend suite (122 tests), `tsc`, `eslint`, and `vite build` all clean.
- **Manual verification against the real running backend** (dev-memory store, real signed OIDC
  tokens via `hack/dev-keycloak.sh`, no fakes): created a real task and pipeline via the actual
  HTTP API, fetched the export endpoint directly, and confirmed the response headers
  (`content-type: application/yaml`) and body (well-formed YAML, faithfully resolving the task
  reference to its real id/name/config) match what the unit tests already proved — not re-testing
  the same thing twice, but confirming the real server (not the test harness's app-under-test
  wiring) behaves identically.

## Not done (by design)

- Import. The brief and ADR 0071 both name this export-only for now, deliberately shaped so a
  future import format is a straightforward addition, not a redesign — not built in this pass.
