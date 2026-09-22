# 0011: Adopting ADR 0063 — storage-backed code + free-text authoring removed from the builder

Status: **built.**

## What changed

- `model.py`: added `StorageCode` — `{type:"storage", backendId, path}`, structurally parallel to
  `CatalogCode` but with no version to pin (a storage object has none; the `{backendId, path}`
  pair *is* the reference). Save-time resolution fills `name`/`language`/`sha256`/`source`, same
  snapshot guarantee as a catalog reference: a run never depends on storage being reachable.
  `Code = CatalogCode | InlineCode | StorageCode`. `InlineCode` itself is unchanged and not
  deleted — its docstring now says plainly that the builder has no path to create new inline code
  any more, but an old pipeline that has it keeps loading, validating and running exactly as
  before.
- `storage_client.py` (new): the save-time client, deliberately tiny and mirroring
  `catalog_client.py`'s shape line for line — one GET, at save time only, with the caller's own
  token, through booth-core's gateway (`/modules/storage/...`, ADR 0007). Infers a language from
  the path's extension (`.py` → python, `.sql` → sql) the same way `CatalogCode.language` is an
  advisory label. Unrelated to `ctx.storage` (ADR 0056/0057) — that is a *running task's own*
  opt-in access as the run's identity; this is how the *service* fetches a task's code before it
  ever runs, using whoever is saving the pipeline's own token. The two never share a code path.
- `service.py`: `_resolve_code` is now generic over `CatalogCode | StorageCode` via a
  `_ref_key(code)` helper (`("catalog", entryId, version)` or `("storage", backendId, path)`)
  instead of being catalog-specific. The trust rule is unchanged and now applies to storage too: a
  client-submitted "already resolved" snapshot is never trusted; a reference is re-fetched unless
  it matches something the *service's own previous version* already resolved, which is also what
  lets an unrelated edit be saved while a dependency is down. `_fetch` was split into
  `_fetch_catalog` (unchanged behaviour) and a new `_fetch_storage`. `PipelineService` gained a
  `storage: StorageClient` field; `app.py` wires it exactly like `catalog` (`StorageClient(cfg.core_url)`,
  closed on shutdown).
- `web/src/types.ts`, `client.ts`: `TaskCode` gained the `storage` member; `api.storageBackends` /
  `api.storageObjects` call booth-storage **directly from the browser** through the gateway
  (`/modules/storage/api/...`), browse-only — exactly the same pattern `catalogCode` /
  `catalogVersions` already use for the catalog. No new booth-pipeline backend route was needed for
  browsing.
- `web/src/components/StoragePicker.tsx` (new): a thin browse-and-pick widget calling booth-storage's
  REST API directly — backend selector, a path-prefix filter (debounced), a list of matching
  objects to "Use". booth-storage's own file browser is **not** reusable here: its published
  package (`@projectbooth/storage-ui`) only exports full page-level apps (`StorageApp` /
  `StorageBrowseApp` / `StorageAdminApp`), confirmed by reading its `src/index.ts` — not a picker
  component — so this follows the same "call the REST API directly" pattern `CodePicker.tsx`
  already established for the catalog, per the ADR's "if possible" qualifier on reuse.
- `web/src/components/TaskPanel.tsx`: the code-source radiogroup is now "From the code catalog" /
  "From storage" — "Write code here" is gone. When a task's code is still `inline` (an old
  pipeline), the panel shows an info banner plus the saved source in a **read-only** textarea, with
  the two radios available to replace it; picking either drops the old inline source, same as
  switching between catalog and storage already dropped whatever was previously picked.
- `web/src/graph.ts`: `newTask()` no longer seeds a new task with `InlineCode` and a starter
  template — those templates (`GENERIC_TEMPLATE`, `KIND_TEMPLATES`) are deleted along with the UI
  path that used them. A new task now starts as an **unresolved `CatalogCode` placeholder**
  (`{type:"catalog", entryId:"", version:"latest"}`), ready for the task panel's picker to fill in.
  `DagCanvas.tsx`'s node label handles all three code types, including "no code picked" for a fresh
  unresolved task.

## Verified

- `test_model.py`: `StorageCode`'s shape and `resolved` property (needs both `source` and
  `sha256`, no version concept to gate on); an old pipeline with `InlineCode` still loads and
  validates.
- `test_api.py`: a full parallel test block to the existing catalog one — storage code is resolved
  and snapshotted at save; a run uses the snapshot and never calls storage again (even if the
  object at that path is later "tampered" with); the call carries the caller's own token and
  workspace; a client-supplied fake snapshot is never trusted; an unrelated edit can be saved while
  storage is down (reusing the service's own previous snapshot), but a *new* storage reference
  cannot be resolved while it is down; storage failure modes (not found, not installed, down) map
  to the same status codes the catalog's do. Also: a language the base runner has no strategy for
  is still refused (unchanged from ADR 0064, message text made generic rather than
  catalog-specific since it now covers storage-sourced code too).
- `PipelineApp.test.tsx`: an old task's inline code renders read-only with no edit path and is
  replaceable via either picker; the storage picker references `{backendId, path}` on save; the
  prefix filter narrows the object list; a missing catalog surfaces a warning and storage still
  works as the alternative (and vice versa) — the builder is never broken by either being down.
- `client.test.ts`: `storageObjects` hits the gateway's `/modules/storage/api` prefix with
  `recursive=true`, browse-only.
- `graph.test.ts`: `newTask()` (with or without a kind hint) now produces an unresolved catalog
  placeholder, never inline source.
- Full suite green: 289 Python (`pytest tests/unit tests/contract`, real Postgres via
  `hack/docker-compose.test.yml`), `ruff check` clean, 123 web (`vitest`), `tsc --noEmit` clean,
  `eslint` clean, `vite build` clean.

## Not done

- No "refresh this storage object's content" affordance was added. Because a storage object (unlike
  a catalog version) has no immutable version to bump, re-picking the exact same `{backendId,
  path}` a pipeline already resolved reuses the cached snapshot rather than re-fetching — the same
  rule that lets an unrelated edit save while storage is down. This is a genuine, self-flagged open
  question: if a workflow turns out to need "pull in the file's latest content at this same path
  without changing the reference," it needs its own decision (an explicit refresh action that
  clears the resolved fields client-side, or something in booth-storage's own versioning if it has
  any) — not guessed at here.
- The storage picker's browsing is prefix-filtered, not a free-text search like the catalog
  picker's — booth-storage's object-listing endpoint takes a `prefix`, not a search query, so that
  is what the picker offers. It requests `recursive=true` unconditionally rather than folder-by-folder
  navigation, since the object listing's JSON shape (`{entries: [{path, ...}]}`, confirmed against
  `tests/unit/test_platform_clients.py`'s real-shaped fixture) does not distinguish files from
  "directories," and assuming an unconfirmed field would have been guessing.
- No migration or bulk-conversion tool was written for pipelines still holding `InlineCode` — the
  ADR's own scope is "no new creation path," not "convert what exists," and nothing asked for one.
