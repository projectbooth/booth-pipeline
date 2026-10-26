# booth-pipeline

Project Booth's pipeline module (nav group **Build**). Draw a **Pipeline** (a DAG of **Tasks**: sources, transforms,
sinks) on a graphical canvas, turn it into a **Job** that runs on demand or on a cron schedule, and read the logs of
every **Run**, per task. Brief: `../booth-architecture/agent-briefs/pipeline.md`. Terminology is fixed by ADR 0010 —
don't rename.

Backed by **Dagster** (ADR 0010), with a **base runner that needs no other module installed** (ADR 0006). Spark is not a
dependency; it is a listed-but-unavailable per-task target until `booth-spark` publishes something to target
([docs/decisions/0006](docs/decisions/0006-spark-runner-unavailable.md)).

> **Read [docs/decisions/](docs/decisions/) before deploying**, especially **[0007](docs/decisions/0007-workload-identity-adoption.md)**:
> each task runs in its own credential-less **Kubernetes Job pod** (ADR 0057/0096, design: [0018](docs/decisions/0018-adr-0096-job-per-task-design.md)) and tasks can use `booth-storage` / the catalog as a
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
  runner_service.py  the service each task's Job pod runs; runners/kubejob.py creates the Job, runners/remote.py drives it (ADR 0096)
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
  `ctx.attempt`, `ctx.trigger` (how the run started; below); the JSON return value (≤ 1 MiB) is the output, and the run keeps up to 64 KiB of it to show editors
  and owners on the run page (viewers don't see outputs, as with a Spark driver log). `print` goes to the task log. A plain script with no
  `run` is executed top to bottom. **Do not put secrets in params** (stored in the spec in clear): use task secrets (below). A task that turns on
  *Platform access* also gets `ctx.storage` (`backends/list/read/read_text/write/delete`) and `ctx.catalog`
  (`datasets/dataset/register_dataset`), acting as the run — see [0007](docs/decisions/0007-workload-identity-adoption.md).
  With `boothDatabase.url` and `core.url` set, such a task also gets `DATABASE_URL` for its workspace's
  booth-database, through booth-core's credential sidecar in its pod — no credential in the URL, a short-lived lease
  issued as the run. **A database connection lasts at most one lease** (an hour today): don't hold one connection across a
  long idle gap or a multi-hour run, and reconnect when it drops (connection lifetime, ADR 0095 fifth amendment).
  With `boothStorage.url` set too, a task whose workspace has a lakehouse warehouse gets s3 credentials the same way,
  via `AWS_SHARED_CREDENTIALS_FILE` / `AWS_CONFIG_FILE`. boto3 reads those itself; for DuckDB, call
  `ctx.s3.duckdb(con)` in a Python task, while a SQL task can query `s3://…` with no setup
  ([0019](docs/decisions/0019-adr-0095-credential-sidecar.md)).
- **Run retention** — by default every run, task run and log line is kept. Set the chart's
  `execution.retention.days` and finished runs older than that (by when they finished) are deleted every night at
  `execution.retention.hourUtc` (08:00 UTC by default), with their task runs and logs; queued and running runs never
  are. It deletes in batches of 500 runs, each its own short transaction that skips rows another replica is pruning
  and gives up on a lock it can't get within 5 seconds, with a pause between batches and at most 30 minutes a night;
  a larger backlog (turning it on over years of history) is finished on the following nights. It runs beside the
  scheduler, never in its way. The Runs tab's counts and success rate then cover only what is kept.
- **Runner images** — a task whose packages are large (pandas, yfinance) can run on an image the operator built
  with them already installed, instead of installing them on every attempt. The operator lists such images in the
  chart's `runner.images`, by name and pinned by digest, and a base task chooses one by name (**Runner image** in its
  settings; checked at save). The task's pod and its pip init container then run that image; the task's own
  requirements still install on top. Build the image from **this release's** image, so its runner service and
  harness match:

  ```dockerfile
  FROM ghcr.io/projectbooth/booth-pipeline:<this release>
  USER root
  RUN pip install --no-cache-dir yfinance==1.7.0
  USER 65532:65532
  ```

  Before a task starts on such an image, the module asks its runner service which protocol revision it speaks; an
  image built from an older or newer release fails the task saying to rebuild it, rather than misreading a skip or a
  secret. Rebuild the images with each booth-pipeline upgrade. An image removed from `runner.images` after a task chose
  it fails that task at start, naming it. Not for Spark tasks, nor for tasks run in-process (local development).
- **Task secrets** (ADR 0128) — an outside key, such as an API key, that a workspace owner sets on the **Secrets**
  page (names only ever come back; editors see the names, viewers nothing). A base Python task declares the ones it
  needs, `secrets: [{name, env}]`, and gets each as that environment variable in its own process, read fresh at each
  attempt. Nothing else receives them: not other tasks, not `invocation.json`, not the pip init container, not a
  Kubernetes object. Values must be at least 8 characters, one line. What the run stores, its log lines, task and run
  errors and kept outputs, has each value replaced by `[secret NAME]`; that can't catch a value split across two log
  lines, a transformed value (base64, URL-encoded), or anything the task sends over the network, so whoever can edit a
  task that declares a secret can read it. Exports carry the names only. A secret the workspace hasn't set fails that
  task ("secret NAME is not set in this workspace"), without retries; the run's other tasks still run. Spark tasks
  can't use secrets (their code is stored in booth-spark). **Setup:** values are sealed (AES-256-GCM) in this module's
  database with a key the operator creates as a Kubernetes Secret and names in `taskSecrets.keySecretName`; see
  `values.yaml`. It is mounted into the API/scheduler pod only, and the module refuses to start with it if tasks would
  run in that pod. Lose the key and every secret must be entered again. Check that your cluster encrypts Secrets at rest.
- **Skipping** — `ctx.skip("reason")` (base and Spark tasks) ends the task *skipped*: nothing to do this time.
  It isn't retried, its downstream tasks don't run, and the run ends **skipped** (amber; "skipped by <task>:
  <reason>"), not failed, unless another task failed. Skipped runs aren't counted in the success rate, and an event
  trigger's `queue_one` follow-up is released by a skipped run as by a succeeded one. A task's own `except Exception`
  doesn't catch the skip.
- **Scheduled time** — a scheduled run records the slot it was for, `scheduledFor` on the run (shown on the run
  page) and `ctx.trigger.scheduled_for` (also `ctx.trigger.scheduledFor`; ISO 8601 UTC; `None` for other runs). A
  run that starts late still knows which slot it is for. **Missed slots are not replayed:** after downtime (or a
  scheduler that fell behind), all the missed slots of a pipeline coalesce into **one** run, whose `scheduledFor` is
  the **most recent** missed slot; the earlier ones never run and aren't recorded. **A slot that finds the previous
  run still active** (and concurrent runs aren't allowed) doesn't run either, but is recorded as a **skipped** run
  ("previous run still active") with that slot, so a dropped minute is visible in the run list.
- **Who a run acts as** (ADR 0058, ADR 0119 amendment 3, ADR 0120). A scheduled run acts as the pipeline's
  *owner*: whoever first saved a schedule onto it, until an editor **takes it over** or a workspace owner gives it to
  a **service account** (`svc:<workspace>:<name>`). A Run-now run acts as whoever pressed the button. Its tasks
  (and, later, its Spark runs) get that identity's access, capped by the pipeline's platform access cap, so the
  owner may be someone who didn't write the tasks' code. A person owner's runs stop getting tokens once they
  haven't signed in for core's `ownerMaxAge` (7 days by default); a service account's don't lapse, but stop at
  once when the account is disabled. Only workspace owners choose or remove a service account; nobody hands a
  pipeline to another person. A workspace owner picks the owner on the pipeline's Schedule tab, from core's list of
  the workspace's accounts; pipeline asks core that the account exists and is enabled before it takes the
  pipeline. A disabled or deleted owning account is badged "Owner disabled" / "Owner not found" for everyone. The
  pipelines list shows which pipelines run as each account: core keeps no such record, and an account must be
  disabled before it can be deleted. **A scheduled run first asks core whether it
  may act for its owner**, even if none of its tasks needs a token: if core refuses (a lapsed or removed person, a
  disabled or deleted account), the whole run fails before any task runs, with the reason. Run-now runs aren't
  checked (the person is present), nor are runs on an install with no workload identity.
- **Event triggers** (ADR 0123; [design](docs/design-event-triggers.md)). Beside its schedule, a pipeline can have
  one event trigger: it runs when files are created in booth-storage under a folder (the Storage page, an app upload,
  the API) or rows are written through booth-api to listed APIs (or every API whose slug starts with a prefix). One
  run per burst: it fires `debounceSeconds` (10) after the last matching event, at most `maxWaitSeconds` (60) after the
  first, never sooner than `minGapSeconds` (5) after its last run. If a run is active and overlapping runs aren't
  allowed, `whenRunning` either runs once more afterwards (`queue_one`, the default) or ignores what arrived during
  it (`skip`). A run acts as the owner exactly like a scheduled run (asked of core at its start), and its tasks get
  `ctx.trigger` (`type`, `events` — the first 100 — and `count`). **That is a hint, not a work list**: delivery is at
  least once and Storage's is best effort, so a task still scans its own inbox and stays idempotent. Writes by the
  pipeline's own runs (`pipeline:<ws>:<runId>`) don't trigger it. Not covered: files written straight to object
  storage (Spark, the lakehouse) and rows written straight to PostgreSQL; keep a slow schedule as the safety net. Paths
  and API names (the last event, a run's events) are shown to editors and owners only. Needs the chart's
  `eventTriggers.enabled` (default on), which declares `events.subscribe` so core grants a bus credential. The bus is
  optional: without it (or with it down) the pod starts, schedules and Run now work, and the Schedule tab says
  "Event bus unavailable" with the reason.
- **Projects** (ADR 0124; booth-architecture `contracts/projects.md`). A pipeline or task can carry one of booth-core's
  projects: a navigation label that grants nothing, and that nothing deciding access, schedules, triggers or run-as reads
  (a unit test holds that line). The slug is checked against core's `GET /api/projects` (cached for a minute) when it is
  assigned or changed; an item keeps a project archived later. Lists take `?project=<slug>`, which includes the
  project's children. If core's registry can't be read, assigning is refused with the reason and nothing else changes:
  lists filter by the exact slug, runs and schedules carry on, and the UI shows nothing about projects. With the
  registry, the pipeline list groups by project (parents, then children; archived projects marked and collapsed; "No
  project" last), honours the shell picker's `?project=`, and offers "Suggest projects from names" (a slug that prefixes
  a name on a `-` boundary; assigned only when confirmed).
- **Spark tasks** (ADR 0119; [design](docs/design-spark-runner.md)). A Python task can run on booth-spark: "Runs on:
  Spark" in its settings, with driver and executor sizes, a few Spark settings, arguments, and the data it may use
  (the workspace's database, its lakehouse, up to 4 storage locations). Its saved code runs as PySpark with the same
  `ctx` (`ctx.spark` is the SparkSession; `run(ctx)` may return up to 64 KiB to downstream tasks), submitted through
  core's gateway **as the run**, so with the access of whoever the run acts as (above). The task's own pod runs no
  user code: it submits, polls and copies the driver log, which only editors and owners can read in the run view
  (viewers see the state). There is no Spark UI link for these runs. A retry after an application that started waits
  at least `runner.spark.retryMinDelaySeconds` (150 s): SeaweedFS keeps an emptied folder about 2 minutes, so a
  write in `errorifexists` mode retried sooner fails. A task meant to be retried should write with `overwrite`.
  Before a retry the previous attempt's application is stopped, so two never write at once. Offered only with
  `core.url` and workload identity, and only in a workspace where booth-spark answers.
- **Python packages** (ADR 0115): a Python task can list requirements (`requests==2.32.3`, one per line; package
  specifiers only, no URLs, paths or pip options). Each run installs them into the task's own pod before its code
  runs, from the install's package index (`runner.pip`), in an init container that holds no token or credentials,
  and the task imports them normally. Nothing is cached between runs. With no route to an index the task fails at
  once, saying which chart value to set ([design](docs/design-task-requirements.md)).
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
| `PUT /api/pipelines/{id}/schedule` | write | `schedule`, `eventTrigger` (left out: kept; null: removed), concurrency, cap, pin |
| `GET /api/pipelines/{id}/event-trigger` | read | the event trigger's state: next fire, pending events, queued follow-up, last event (its path or API for editors and owners only) |
| `GET /api/runs` `?jobId&status` · `GET /api/runs/{id}` · `POST /api/runs/{id}/cancel` | read · write | run detail includes per-task state, an event-fired run's `triggerEvents` (count; the events for editors and owners) and, for editors and owners, each task's `output`: `{value}`, or `{omitted: tooLarge\|unstorable, bytes}` when not kept |
| `GET /api/runs/{id}/logs` `?task&after&limit` | any | poll with the last `seq`; `done` means finished *and* nothing more can arrive |
| `GET /healthz` · `GET /livez` | none | readiness (own database only — what core polls) · liveness |

`editor`/`owner` write and run; `viewer` reads (mirrors ADR 0038/0048). **Roles are re-derived from the token's `groups`
claim, never trusted from `X-Booth-Role`** — an over-claiming header is rejected 403 (ADR 0041; verified against real
Keycloak).

## Running and testing

```sh
pip install -e ".[dev]"
docker compose -f hack/docker-compose.test.yml up -d --wait      # PostgreSQL on :15434, NATS JetStream on :14222
eval "$(sh hack/test-env.sh)"
pytest tests/unit tests/contract                                  # the CI layers 1-2 (needs helm)
(cd web && npm ci && npm run typecheck && npm run lint && npm test -- --run && npm run build)
```

Without Postgres (or NATS) the store (or event-bus) tests **skip** locally; CI sets `BOOTH_TEST_REQUIRE_EMULATORS=1`, which turns a missing
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

## Operations

**Where token-signing keys come from (`oidc.jwksUrl`, env `BOOTH_OIDC_JWKS_URL`; ADR 0108).** Empty (the
default, and the right setting for any external identity provider), the module finds the provider's keys through its
discovery document at `oidc.issuerUrl`. Set, it skips discovery and fetches keys directly from that URL; a token's
`iss` is still checked exactly against `oidc.issuerUrl`, and setting it without `oidc.issuerUrl` stops the module at
startup. The startup log names the issuer and where keys come from. The bundled install sets it to Keycloak's
in-cluster Service over plain http, so no pod has to reach the Ingress or trust its certificate. That fetch is
unauthenticated and unencrypted: it trusts the cluster network, so it relies on NetworkPolicy keeping anything else
from answering at that address. Don't point it outside the cluster.

**Where a task's packages come from (`runner.pip`; ADR 0115 and its amendment 1).** An in-cluster index (a mirror or
pull-through proxy) is the narrow path: set `runner.pip.indexUrl` to it and `runner.pip.egress` to its pods'
selectors and port, and task pods may reach it with `allowInternet` still off. Public PyPI (`indexUrl` empty) needs
`runner.networkPolicy.egress.allowInternet=true`; the installer then reaches what task code already reaches, never the
private ranges. An index on a private address needs `runner.networkPolicy.egress.extra`. NetworkPolicy is per pod,
so task code can reach the index too. `runner.pip.timeoutSeconds` (300) bounds the whole install and is added to the
time a task pod may take to start; `runner.pip.sizeLimit` (1Gi) is the install volume's size.

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
