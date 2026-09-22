# booth-pipeline

Project Booth's pipeline module (nav group **Build**). Draw a **Pipeline** (a DAG of **Tasks**: sources, transforms,
sinks) on a graphical canvas, turn it into a **Job** that runs on demand or on a cron schedule, and read the logs of
every **Run**, per task. Brief: `../booth-architecture/agent-briefs/pipeline.md`. Terminology is fixed by ADR 0010 —
don't rename.

Backed by **Dagster** (ADR 0010), with a **base runner that needs no other module installed** (ADR 0006). Spark is not a
dependency; it is a listed-but-unavailable per-task target until `booth-spark` publishes something to target
([docs/decisions/0006](docs/decisions/0006-spark-runner-unavailable.md)).

> **Read [docs/decisions/](docs/decisions/) before deploying**, especially **[0007](docs/decisions/0007-workload-identity-adoption.md)**:
> task code runs in a separate, credential-less **runner pod** (ADR 0057) and tasks can use `booth-storage` / the catalog as a
> short-lived run identity (ADR 0056/0058), called through booth-core's gateway (`BOOTH_CORE_URL/modules/{storage,catalog}`,
> which accepts workload tokens since ADR 0059). `BOOTH_PIPELINE_STORAGE_URL` / `_CATALOG_URL` remain as an escape hatch only.

## What v0 delivers

| Definition-of-done item | Where |
|---|---|
| Graphical DAG builder (React Flow canvas): source → transform(s) → sink, add/connect/delete/rename, tidy layout, live server-side validation, immutable versions | `web/src/views/PipelineEditor.tsx`, `components/DagCanvas.tsx`, `graph.ts` |
| Per-Task config: code (catalog reference or inline), runner (base by default), retry override, dependencies, timeout, params | `web/src/components/TaskPanel.tsx`, `src/booth_pipeline/model.py` |
| Pipeline → Job: run on demand and cron schedule (IANA timezone, pause), pin a version or follow latest, default retry, persisted and re-editable | `service.py`, `scheduler.py`, `web/src/views/JobViews.tsx` |
| Run logs per Job run and per Task, through the shell (live, cursor-polled) | `runs.py`, `api.py` (`/runs/{id}/logs`), `web/src/views/RunView.tsx` |
| Manifest + health check per contract | `charts/booth-pipeline/templates/boothmodule.yaml`, `/healthz`, `tests/contract` |
| CI per `contracts/testing-strategy.md` | `.github/workflows/` |

## Stack

Sibling modules are Go; this one is **Python** because Dagster is (FastAPI + psycopg + PyJWT), with a **React + TypeScript
+ Vite + Tailwind** UI in `web/` published as `@projectbooth/pipeline-ui` (ADR 0030), a Helm chart with a `BoothModule`
manifest (ADR 0019), and PostgreSQL via the core-provisioned `booth-database-credentials` Secret (ADR 0053).

```
src/booth_pipeline/
  model.py         the domain: PipelineSpec/Task/CodeRef/Schedule + the DAG rules (pure, no I/O)
  engine.py        compiles a spec to a Dagster job and runs it; retries/ordering are Dagster's
  runners/         the seam: Runner protocol, the base subprocess runner, the registry
  workload.py      minting client (API pod only) + per-run access provider (ADR 0056/0058)
  runner_service.py  the credential-less runner pod's service; runners/remote.py is its client (ADR 0057)
  runs.py          StoreRecorder (task state + buffered, capped logs) and RunManager (workers, heartbeat, cancel, sweep)
  service.py       every use case, independent of HTTP (save = resolve code -> validate -> compile -> store)
  scheduler.py     cron polling with atomic claiming; safe in every replica
  store/           Store protocol + memory + PostgreSQL implementations, ONE shared contract suite
  auth.py          token verification + role re-derived from the token (ADR 0025/0041)
  catalog_client.py  the only place we talk to booth-catalog (save-time snapshot, caller's own token)
  api.py app.py    HTTP
web/               the native UI package
charts/            Helm chart + BoothModule
docs/decisions/    judgment calls the ADRs didn't settle — READ THESE
```

## Concepts

- **Pipeline** is versioned; every save is a new *immutable* version holding the graph **and a snapshot of all code**.
- **Job** follows the pipeline's latest version or is pinned to one. Each **Run** records the version it ran.
- **Task code** — define `run(ctx)`; `ctx.inputs[<upstream key>]` are upstream return values, `ctx.params`, `ctx.log`,
  `ctx.attempt`; the JSON return value (≤ 1 MiB) is the output. `print` goes to the task log. A plain script with no
  `run` is executed top to bottom. **Do not put secrets in params** (stored in the spec in clear). A task that turns on
  *Platform access* also gets `ctx.storage` (`backends/list/read/read_text/write/delete`) and `ctx.catalog`
  (`datasets/dataset/register_dataset`), acting as the run — see [0007](docs/decisions/0007-workload-identity-adoption.md).
- **Catalog code** is referenced as `{entryId, version}` and snapshotted at save; runs never call the catalog
  ([0001](docs/decisions/0001-code-catalog-reference.md)).

## API (through core's gateway at `/modules/pipeline/api/...`)

Every request needs `Authorization: Bearer …` and `X-Workspace`. Errors are `{"error", "field"?}` (422 validation naming
the field, 409 conflict, 404, 403, 503 dependency down). Lists take `?q&limit&offset` → `{"items","total"}`.

| Route | Role | |
|---|---|---|
| `GET /api/runners` · `GET /api/config` | any | available runners (Spark listed, unavailable, with why) · limits |
| `GET/POST /api/pipelines` · `GET/PUT/DELETE /api/pipelines/{id}` | read · write | delete is 409 while jobs exist |
| `POST /api/pipelines/validate` | any | live check for the canvas; never saves, never calls the catalog |
| `GET/POST /api/pipelines/{id}/versions` · `GET …/versions/{n\|latest}` | read · write | save = new immutable version |
| `GET/POST /api/jobs` · `GET/PUT/DELETE /api/jobs/{id}` · `POST /api/jobs/{id}/run` | read · write | run → 202 queued; 409 if one is active and overlap isn't allowed |
| `GET /api/runs` `?jobId&status` · `GET /api/runs/{id}` · `POST /api/runs/{id}/cancel` | read · write | run detail includes per-task state |
| `GET /api/runs/{id}/logs` `?task&after&limit` | any | poll with the last `seq`; `done` means finished *and* nothing more can arrive |
| `GET /healthz` · `GET /livez` | none | readiness (own database only — what core polls) · liveness |

`editor`/`owner` write and run; `viewer` reads (mirrors ADR 0038/0048). **Roles are re-derived from the token's `groups`
claim, never trusted from `X-Booth-Role`** — an over-claiming header is rejected 403 (ADR 0041; verified against real
Keycloak).

## Running and testing

```sh
pip install -e ".[dev]"
docker compose -f hack/docker-compose.test.yml up -d --wait      # PostgreSQL on :15434
eval "$(sh hack/test-env.sh)"
pytest tests/unit tests/contract                                  # the CI layers 1-2 (needs helm)
(cd web && npm ci && npm run typecheck && npm run lint && npm test -- --run && npm run build)
```

Without Postgres the store tests **skip** locally; CI sets `BOOTH_TEST_REQUIRE_EMULATORS=1`, which turns a missing
database or `helm` into a failure. Beyond CI:

```sh
hack/dev-keycloak.sh up                       # real Keycloak (the hub's booth-local realm) + password grants
docker build -t booth-pipeline:dev .
BOOTH_TEST_KEYCLOAK_URL=http://localhost:8081 pytest tests/integration
```

That runs the real app against real Keycloak + PostgreSQL (real tokens, the forged-role attack, persistence across a
restart, two processes racing one scheduled fire) and executes tasks **inside the chart's lockdown** (non-root,
read-only root filesystem).

Local run: `BOOTH_PIPELINE_DEV_MEMORY=true BOOTH_OIDC_ISSUER_URL=… BOOTH_OIDC_REQUIRE_AUDIENCE=false python -m booth_pipeline`
(in-memory, vanishes on exit). `web`'s `npm run dev` is a harness: paste a token (`hack/dev-keycloak.sh token bob`); its
Vite proxy strips the gateway prefix and turns `X-Workspace` into `X-Booth-Workspace`, as the real gateway does.

## Wiring into booth-design

Add `@projectbooth/pipeline-ui`, import `…/dist/style.css`, and `registerNativeModule("pipeline", PipelineApp)`. The
manifest's `navPath` is `/pipeline`; the UI reads its own sub-route from the address bar (the props carry no route —
same workaround as catalog/storage).

## Not done / known limits

- **The real React Flow canvas has not been exercised in a real browser.** Browser automation was unavailable while
  building; component tests replace the canvas with a stub, and the pure graph logic, API client and build are tested.
  Drag/connect/delete/zoom and layout on the actual canvas, dark mode, and the shell mount are **unverified**.
- **`integration.yml` (kind) has never run**, and the module has never been installed by a real `booth-core` or mounted in
  a real `booth-design`. Branch protection on `main` is a GitHub setting and has not been configured from here.
- **Workload identity is tested against fakes** of core, storage and catalog, never a real core minting or a real module trusting
  its issuer ([0007](docs/decisions/0007-workload-identity-adoption.md)) — the gateway path (ADR 0059) is checked only against a fake gateway that mimics core's prefix stripping. Runner isolation
  depends on a CNI that enforces NetworkPolicy, and the runner is a single replica (token refresh is pushed to the pod hosting a task).
- Runs execute **serially** ([0003](docs/decisions/0003-dagster-usage.md)); the base runner runs Python only, using
  what is installed in the image (no per-task dependencies).
- Restarting a replica cancels its running tasks (they show `canceled`); runs are not migrated between replicas.
- Log retention: none (a run's logs live as long as its job); capped at 50,000 lines per run.
- Unsaved canvas edits are protected against tab close/reload, not against in-app navigation.
- No accessibility audit beyond labelled controls, roles and a table/checkbox alternative to the canvas for status and
  wiring; canvas node dragging itself is pointer-only.
