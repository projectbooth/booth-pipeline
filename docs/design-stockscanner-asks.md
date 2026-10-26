# Design note: what StockScanner (ADR 0127, 0128) needs from booth-pipeline

Status: **for coordinator review**, 2026-10-26. No code, no PR.

Inputs:
- booth-architecture ADR 0127, items 2, 4 and 5 and both 2026-10-26 amendments;
- ADR 0128, 0096, 0115 and 0065;
- this repo at `aab6e65` (main, pipeline-ui 1.6.0), and booth-catalog at `c898575`.

Each answer says what is **verified** (from code or a run, cited) and what is **not**, then gives a
recommendation.

Measurements were made locally, not on the homelab, and changed nothing there:
- a yfinance install inside the image the homelab runs (`ghcr.io/projectbooth/booth-pipeline:0.2.1`);
- the run-list queries against this repo's own PostgresStore, with a year of minute-cadence runs seeded into
  the local test database (emptied afterwards);
- the task pod cold start, read from Integration's own measurement on main.

## Summary

| # | Ask | Today | Recommendation | Effort |
|---|---|---|---|---|
| 1 | Task secrets | Workable as ADR 0128 says, with three refinements | Build it; base tasks only in v1 | ~1 to 1.5 weeks |
| 2 | Package installs | yfinance: about 10 s and 224 MB **per attempt** | Operator-built runner images, chosen per task | ~3 to 4 days |
| 3 | Retention | **Nothing is ever pruned**; queries stay fast, storage grows | A retention setting and a nightly prune | ~2 days |
| 4 | Skipped status | Doesn't exist; `skipped` is already a task status | `ctx.skip(reason)` and a terminal `skipped` run status | ~2 to 3 days |
| 5 | Scheduled time | Not recorded, not in `ctx` | Record it on the run; `ctx.trigger.scheduledFor` | ~0.5 day |
| 6 | Cold start / concurrency | Pod Ready in a median 2.0 s (CI); 4 runs at a time per replica | Measure on the homelab at the G1 gate; raise `maxWorkers` | measurement |
| 7 | Cluster load | Every Job deleted after its attempt; Secret owned by the Job; TTL 300 s backstop | Watch list below; no change needed first | none |
| 8 | Parameters | Node `paramOverrides` are versioned with the pipeline | Use them; runs need a saved version | none |
| 9 | Catalogued code | Pinned catalog reference exists; no import of shared code | "Shared modules" from the catalog, snapshotted at save | ~3 days |

## 1. Task secrets (ADR 0128)

**Verified: how a task's input reaches its pod today.**
- **Route:** `KubernetesJobRunner` (`runners/kubejob.py`) creates a Job and a per-attempt Secret holding only a
  random runner bearer. The Secret is owner-referenced to the Job (`_secret`, `ownerReferences`). It then sends
  the whole invocation to the pod's runner service over HTTP with that bearer (`RemoteRunner`, `runners/remote.py`).
- **Request body:** `runId`, `source`, `params`, `inputs`, `access` (the platform token) and `trigger`, built in
  `remote.py`.
- **In the pod:** `runner_service.py` (`RunBody`) hands it to `SubprocessRunner`, which writes `invocation.json`
  and runs the harness under `python -I`.
- **Precedent:** the platform token already travels exactly this way, in-cluster, plaintext HTTP to the pod's
  IP. The task NetworkPolicy admits only the API pod.

**Design (ADR 0128 as written works, with three refinements).**
- **Storage:** a `task_secrets` table in the module's own database: `(workspace, name, ciphertext, nonce,
  key_id, updated_by, updated_at)`.
  - **Encryption:** AES-256-GCM with the workspace and name as associated data. `cryptography` is already a
    dependency, through `pyjwt[crypto]`.
  - **The key:** lives outside the database, in a Kubernetes Secret the operator creates
    (`booth-pipeline-secrets-key`, a chart value naming it). It's mounted into the API pod only, like the
    minting credential, and `config.py` refuses to start if tasks run in-process (the rule that already guards
    the minting and bus credentials).
  - **Rotation:** `key_id` allows rotating the key without losing values.
  - **etcd:** values never enter etcd. Only the key does, which is why etcd encryption at rest still matters,
    as ADR 0127 item 5 says.
- **Declaration:** the task declares `secrets: [{name, env}]` (versioned with its config). The engine resolves
  and decrypts the declared secrets at the attempt's start and adds them to the run request as
  `env: {ENV: value}`. The runner service sets them only in the task subprocess's environment.
  - **Pip container:** the pip init container is a different container, never in the request, so it never
    receives them.
  - **Disk:** nothing is written to disk. `invocation.json` must not carry them, which is a change.
- **Redaction:** at the three places a value can be stored.
  - **Log lines:** `StoreRecorder._emit` in `runs.py`, the one path every log line takes, including the Spark
    driver-log copy.
  - **Errors:** a task attempt's `error` and the run's `error`.
  - **Outputs:** the stored output (`kept_output`).

  Each exact value is replaced by `[secret NAME]`.
- **Refinement 1, values too short to redact:** refuse secrets shorter than 8 characters. Redacting a short or
  common value would mangle unrelated text.
- **Refinement 2, base tasks only in v1:**
  - A Spark task's code runs in booth-spark's driver, and its source is submitted to booth-spark as
    `inlinePython` (`_spark_client.py`), which booth-spark stores. A value placed there leaves this module's
    control.
  - Spark secrets need booth-spark to accept a secret reference. That's a separate design.
- **Refinement 3, export and missing secrets:** export carries secret names only. A missing secret at run time
  fails that task with "secret NAME is not set in this workspace", not the run's other tasks.
- **Who and what's shown:** only workspace owners write secrets; the API never returns a value. The run view
  lists the secret names an attempt used.

**Not covered by redaction (stated in the UI):**
- a value split across two log lines;
- a value printed in another encoding (base64, URL-encoded);
- a value a task sends to the network. ADR 0128 already says the control is who may author tasks.

**Not verified:** etcd encryption at rest on the homelab cluster. That's an operator check, as ADR 0127 item 5
says.

**Effort and risks.** About 1 to 1.5 weeks: table, encryption, API, owner UI, task declaration, delivery,
redaction, tests (including a test that no value reaches logs, outputs, errors, exports or `invocation.json`).
- **Main risk: losing the key.** Every secret becomes unreadable and must be re-entered; the UI must say so.
- **Second risk:** a task printing its secret in a transformed form. Unavoidable, and documented.

## 2. Package installs (ADR 0115)

**Verified: how installs work.**
- Every attempt of a task with requirements runs its own pip init container into an emptyDir (`site`,
  `runner.pip.sizeLimit: 1Gi`). There's no cache by design in v0 (ADR 0115).
- Routes are `index`, `internet`, `extra` or `unrestricted` (`PIP_ROUTES`, `kubejob.py`).

**Measured: a cold `pip install yfinance` inside `booth-pipeline:0.2.1`** (Python 3.13.16, pip 26.2.1), from
public PyPI over this workstation's connection, three runs:

| Run | Time | Written |
|---|---|---|
| 1 | 11.2 s | 224 MB |
| 2 | 10.1 s | 224 MB |
| 3 | 9.8 s | 224 MB |

- **What it brings:** yfinance 1.7.0 installs 23 packages. The largest are pandas 80 MB, numpy 43 + 27 MB
  (libs) and curl_cffi 41 MB.
- **Import:** `import yfinance` then takes 0.45 s.
- **Not measured:** the homelab's own connection, and its pip route (I didn't look at the homelab).

**Cost at ADR 0127's cadences.** Options capture is roughly 15 tickers every 5 or 15 minutes, plus SPY and QQQ
most minutes, in market hours. That's about 800 to 1,000 attempts a day. At 224 MB and 10 s each, that is about
200 GB of downloads and emptyDir writes a day, and 10 s added to every capture.

**Options:**
- **An install cache** (a shared wheel cache or a caching proxy index): saves the download but still unpacks
  224 MB per attempt, so maybe 4 to 6 s left (not measured). A shared writable cache volume across task pods
  also breaks the isolation ADR 0096 keeps.
- **An in-cluster caching index** (`runner.pip.indexUrl` plus `runner.pip.egress`, already supported): same
  unpack cost, and an operator-run service.
- **Recommended: operator-built runner images, chosen per task.**
  - **Chart:** a value maps names to images pinned by digest, for example
    `runner.images: {stocks-py: ghcr.io/…@sha256:…}`. The default image is unchanged.
  - **Task:** declares `runnerImage: stocks-py`. Validated at save against the list; the Job template's image is
    swapped for that task only.
  - **Requirements:** still work on top, for small additions.
  - **The image:** built from the module's image (same runner service and harness) plus yfinance, by the
    StockScanner repo, published like the module image.
  - **Result:** zero install time, nothing written per attempt, and `IfNotPresent` keeps it on the node after
    the first pull.
  - **Effort:** about 3 to 4 days (chart value, model field, Job image override, a check that the image's runner
    protocol version matches, UI field, tests).
  - **Risk:** an operator image with an old runner service. The protocol check refuses it with a clear message.

## 3. Run and log retention

**Verified: nothing is pruned.**
- No code deletes runs, task runs or log lines. The only pruning in the module is of event-trigger bookkeeping
  (`event_bus.py`).
- A run's logs are capped at `max_log_lines_per_run` = 50,000 lines (`config.py`).
- Deleting a pipeline cascades its runs.

**Measured: the run list after one year at minute cadence.**
- **Seeded:** 300,000 runs, 400,000 task runs and 9.6 M log lines (24 short lines per task run) in one
  workspace, through `PostgresStore` against the local test PostgreSQL 16 (`hack/docker-compose.test.yml`).
- **Sizes:**

  | Table | Size |
  |---|---|
  | `runs` | 113 MB |
  | `task_runs` | 78 MB |
  | `run_logs` | 3.0 GB |

  Real log lines are longer: expect several GB a year.
- **Query times** (median of 5; each list call includes its `count(*)`):

  | Query | Median |
  |---|---|
  | Workspace newest 200 (the pipeline list's bulk call, polled every 10 s while a run is active) | 45 ms |
  | Minute pipeline, newest 50 (Runs tab) | 14 ms |
  | Last succeeded / last failed (Runs tab) | 15 / 14 ms |
  | Workspace failed, newest 50 | 24 ms |
  | Page at offset 100,000 | 42 ms |
  | One run's logs | 1 ms |

**So:**
- **The run list stays usable:** status filtering and newest-first ordering work, and every query is under
  50 ms.
- **The cost is storage:** the module's database sits on core's bundled PostgreSQL and its disk, shared with
  everything else.

**Recommendation.** A retention setting (chart value `execution.retention.days`, default unset = keep, today's
behaviour) and a nightly prune in the scheduler loop that deletes finished runs older than that, in batches.
- **Deletes:** task runs and logs cascade. Stats keep counting only what's kept.
- **For StockScanner:** 30 days covers the parallel-run comparison (ADR 0127 item 7).
- **Effort:** about 2 days, including a store contract test and a batch-size bound so a prune never holds long
  locks.

## 4. A real "skipped" run status

**Verified: what exists.**
- Run statuses are `queued`, `running`, `succeeded`, `failed` and `canceled` (`records.py`). The database has no
  CHECK constraint on them.
- `skipped` already exists as a **task** status (an upstream failed). The UI already styles it
  (`primitives.tsx`, amber "Skipped").
- There's no failure alerting in booth-pipeline (grep: no alert, webhook or notification code). "Not counted as
  a failure" means only that runs lists, filters and the pipeline's last-run status don't show it as failed.

**Design.**
- **In the task:** `ctx.skip(reason)` raises a `TaskSkipped` the harness reports in its result, alongside
  output and error.
- **In the engine:** the task ends `skipped` with the reason, its downstream tasks are `skipped`, and the run
  ends `skipped`, a new terminal status. That needs the Dagster op to yield no output, so downstream steps skip,
  rather than raise.
- **Everywhere else:** the store, API status filter and UI badge and filter add it. The last-run status shows
  it in amber. The event trigger's last-fired and queue logic treats it like succeeded.
- **Spark:** the Spark prelude gets the same `ctx.skip`, as a marker line like its output line.
- **Effort:** about 2 to 3 days with tests.

## 5. Scheduled fire time in the task context

**Verified: not available.**
- A run's `created_at` is when the run row was written (`service.start_run`).
- `ctx` has `run_id`, `task_key`, `attempt`, `params`, `inputs` and `trigger`. `ctx.trigger` has `type`,
  `events`, `count` and `missedEvents` (`_harness.py`, `runs.run_trigger`).
- The fire time is known at claim time: `claim_due_pipelines` returns the pipeline as read before it advances
  `next_run_at` (`store/postgres.py`). It's simply not passed on.

**Recommendation.**
- **Record it:** a `runs.scheduled_for` column, set from the claimed `next_run_at` for schedule-fired runs.
- **Expose it:** as `ctx.trigger.scheduledFor` (ISO 8601 UTC), also shown on the run page. A late pod then still
  knows which minute it captures.
- **Missed fires aren't replayed:** after downtime the claim coalesces them into one fire (ADR 0065), so one
  run's `scheduledFor` is the most recent missed slot. This should be stated.
- **Effort:** about half a day.

## 6. Cold start and concurrency

**Verified: one part measured.**
- **What was measured:** Integration's `coldstart` step on main (run 40229505015, kind on a GitHub runner, image
  preloaded): from Job created to pod Ready, a median of **2.0 s** and a max of 3.0 s over 5 sequential tasks.
  The task's subprocess then takes a median of **0.06 s**.
- **ADR 0065's figure:** about 0.2 s of in-process engine overhead per trivial run.
- **The full path from schedule fire to the task's process starting:**
  1. the scheduler's index wakes at `next_run_at`;
  2. claim (one transaction);
  3. the run row;
  4. a worker thread;
  5. task configs resolved;
  6. the start-time check with core (one mint round trip; `_authorize`, retried after 5 s and 10 s if core is
     unreachable);
  7. Dagster compile;
  8. a Job slot;
  9. Job and Secret created;
  10. pod Ready;
  11. the run request;
  12. the subprocess.

  Estimated at **3 to 5 s** before task code runs, plus any pip install. **Not measured end to end, and not
  on the homelab.**

**Verified: limits.**
- **`execution.maxWorkers` = 4:** runs executing at once per API replica (`values.yaml`). A fifth waits
  `queued`, failing after `queued_timeout_seconds` = 900.
- **`runner.maxConcurrent` = 16:** outstanding task Jobs per replica. Tasks within a run run serially (ADR 0003),
  so with 4 workers at most 4 Jobs are in flight from one replica.
- **A fire that finds the previous run active is skipped,** logged at INFO only and recorded nowhere
  (`service.start_scheduled`), unless `allowConcurrentRuns` is set.

**Recommendation.**
- **Measure at the G1 gate on the homelab:** fire to first log line, from run logs: `task pod ready in X s` is
  already logged, and `scheduledFor` (item 5) would make the delay exact.
- **Raise `maxWorkers`** to the number of concurrent capture pipelines plus headroom (8 is a reasonable start).
- **Record skipped fires** as a run (status `skipped`, reason "previous run still active"), so a dropped minute
  is visible. This falls out of item 4.

## 7. Cluster load and garbage collection

**Verified.**
- **Each attempt:** one Job (with its pod) and one Secret.
- **Cleanup:**
  - the API pod deletes the Job when the attempt ends, with Background propagation (`kube.py` `delete_job`);
  - the Secret is owner-referenced to the Job, so it's garbage-collected with it;
  - backstops: `ttlSecondsAfterFinished: 300` (`runner.ttlSecondsAfterFinished`) and an `activeDeadlineSeconds`
    of the timeout + start timeout + 120 s, plus pip time (`kubejob.py`).
- **In Integration:** the "Finished tasks leave no Job, pod or Secret behind" step checks this.
- **Per attempt, API calls:** create Job, create Secret, poll pods until Ready, delete Job; the controller adds
  pod writes and Events.

**Not verified:** the homelab's datastore type, etcd or kine/SQLite size and compaction, or API server latency.

**What to watch at 1,600 attempts a day:**
1. leftover Jobs, pods and Secrets in the module namespace (should be about zero between runs);
2. datastore size before and after a day, and whether compaction and defragmentation run;
3. API server request latency (p99 for create and delete) and the Events count (default Event TTL is 1 hour);
4. node ephemeral storage: emptyDir writes, 224 MB per options attempt today, gone with runner images (item 2);
5. kubelet image and container GC on the node.

ADR 0127 amendment 10 lists the first three for G1. Nothing to change before measuring.

## 8. Parameters and their history

**Verified.**
- A node's `paramOverrides` (ADR 0078) are part of the pipeline spec (`TaskRef.param_overrides`, `model.py`), so
  every saved pipeline version records them, and the YAML export shows them.
- At run time they're merged shallowly over the task's own `params` (`resolve.py`).
- Editing them doesn't touch task code.
- Plain Save writes only the pipeline draft. Runs use the pinned or latest **saved version**, never the draft
  (`service.start_run`), so a parameter change takes effect only through "Save as new version". Each run records
  the version it ran, so which tickers a run used is traceable.
- There are no run-time parameters (a run can't be started with different values).

**Recommendation.** Use node overrides for `tickers`, with each change saved as a new version with a note. A
one-pipeline-wide parameter, so one `tickers` value feeds several nodes, isn't supported. Each node carries its
own override; that's fine for one capture task per pipeline. No change needed.

## 9. Catalogued code and shared code at run time

**Verified.**
- **booth-pipeline:** a task's code can be `{type: catalog, entryId, version}` (`CatalogCode`, `model.py`;
  `docs/decisions/0001`). `latest` is pinned at save, and the source, name and sha256 are snapshotted into the
  task version, so a run never calls the catalog.
- **booth-catalog** (`internal/code/model.go`): a registry of versioned, immutable single-file sources up to
  1 MiB each. Its own comment: "no execution, no packaging, no dependency resolution".
- **Not supported:** importing anything besides the task's own source and its pip requirements.

**Smallest change: shared modules from the catalog.**
- **Declaration:** a task declares `modules: [{entryId, version, name}]`.
- **At save:** each is resolved and snapshotted exactly like `CatalogCode`. Pinned, recorded and exported, with
  the same byte budget (`MAX_INLINE_SOURCE_BYTES` = 256 KiB in total).
- **At run time:** the harness writes each snapshot to `modules/<name>.py` beside `task.py` and puts that folder
  on `sys.path`, so `import market_calendar` works.
- **What it gives StockScanner:** ADR 0127's `market_calendar` module lives once in the catalog, versioned.
  Each pipeline pins a version, so changing it never alters a running pipeline silently.
- **Effort:** about 3 days (model, save-time resolution, run request, harness, Spark prelude, UI picker, tests).

**Not chosen:** a private package index.
- It's the more general route, since the in-cluster index route already exists (`runner.pip.egress`).
- But it needs a publishing pipeline and an index service the homelab doesn't run, and it brings back install
  time per attempt.
- It could come later for multi-file packages.

## Deltas while building

Changes from the note above, found while building it. Each PR adds its own.

**PR 1, skip and scheduled time:**
- **A coalesced fire's slot.** The note said the claimed `next_run_at` is the most recent missed slot. It's the
  oldest: the claim advances `next_run_at` from now, so after downtime the stored value is still the first missed
  slot. The service computes the most recent slot due at the claim (`schedule.latest_slot`, croniter's `get_prev`
  for cron, arithmetic for intervals), so `scheduledFor` is what the note promised. Earlier missed slots never run
  and aren't recorded.
- **Names and format.** In Python it's `ctx.trigger.scheduled_for`, like `missed_events`, with `scheduledFor` as an
  alias. Times are written as the API writes every time, `2026-10-28T13:15:00+00:00`, not with `Z`.
- **A dropped fire's run** is created already finished (`skipped`, "previous run still active", that slot), with
  its tasks `skipped` and one system log line. It never queues, and it doesn't count as active.
- **Mixed outcomes.** A skip and an independent branch that succeeds: the run is `skipped`. A skip and a failure:
  `failed`.
- **The skip can't be swallowed.** In the task's process, `ctx.skip` raises a `BaseException`, so a task's own
  `except Exception` doesn't catch it.
- **`execution.maxWorkers`** goes to 12, not the note's 8, for StockScanner's dozen quarter-hour pipelines. The
  database pool now grows with it (maxWorkers + 8 per replica, at least 10).

**PR 2, task secrets:**
- **The key Secret's shape.** One data key per key id, each holding 32 random bytes in base64
  (`--from-literal=k1="$(openssl rand -base64 32)"`). The chart values are `taskSecrets.keySecretName` (empty means
  off) and `taskSecrets.keyId`, which names the sealing key when the Secret holds more than one. It's mounted at
  `/etc/booth/task-secrets-key`, in the API pod only.
- **Rotation is automatic once keys are in place.** At startup, values sealed with any other mounted key are
  re-sealed with the current one. Values whose key is gone are left and counted in a warning; they must be
  re-entered.
- **Editors can list the names,** so they can declare them on tasks. Viewers can't. Only owners set, replace or
  delete.
- **Values must be one line,** with no surrounding whitespace, and at most 8 KiB, beyond the note's 8-character
  minimum.
- **Python base tasks only.** A SQL task is refused as well as a Spark one, because it has no environment to read.
  Environment variables the runner sets, or that change how the process runs, are refused at save (`PATH`,
  `BOOTH_*`, `PYTHON*`, `AWS_*`, `DATABASE_URL` and the like).
- **The run request carries `env` only when a task has secrets,** so a task without any still runs on a runner
  image from before this change.
- **One more redaction point.** A failure message carrying a value is redacted before Dagster logs it in the API
  pod's own log.
- **A missing secret** fails that task without retries; its downstream tasks don't run, and independent tasks do.
- **The run view** shows, on each task, the secret names its latest attempt got (`task_runs.secrets`).

**PR 3, runner images per task:**
- **The protocol check** is a new authenticated `GET /v1/protocol` on the runner service. It returns the protocol
  revision (2: skip results and `env`; 1 is everything before) and the release. It's asked only for a task on an
  operator image, and the revision must match exactly: older and newer are both refused, saying to rebuild from
  this release. An image too old to have the endpoint answers 404 and is refused the same way. Asking doesn't use
  up a one-shot pod.
- **The pip init container runs the chosen image too,** so requirements on top install with the image's own
  interpreter. Credential sidecars keep their images.
- **Not for Spark tasks:** their pod runs only the module's Spark client. Refused at save.
- **An image removed from the chart after save** fails the task at start, before any Job, naming it. The builder
  keeps showing it, marked "no longer configured".
- **Rendering refuses a tag** (only a digest), or a name that isn't lower-case letters, digits and `-`. Startup
  refuses them too.

**PR 4, retention:**
- **Its own thread,** started with the scheduler, rather than inside the scheduler loop. The first prune after
  turning retention on can take many nights' budgets, and it must never delay a fire. It runs at
  `execution.retention.hourUtc`, 08:00 UTC by default (03:00 or 04:00 in Toronto).
- **The bounds:**
  - 500 runs per transaction, oldest first by `finished_at`, using `FOR UPDATE SKIP LOCKED`;
  - `lock_timeout` 5 s and `statement_timeout` 120 s per batch;
  - a 0.5 s pause between batches, and a 30-minute budget per night.
  Every replica prunes, and `SKIP LOCKED` splits the work.
- **The cutoff is `finished_at`.** Queued and running runs are never pruned, however old.
- **A new partial index** on `runs (finished_at)` (migration 0013), so a batch finds its rows without a scan.

**PR 5, shared modules:**
- **The snapshot.** Each module stores what `CatalogCode` stores: `entryName`, `language`, `sha256` and `source`,
  filled at save. A module the task's previous version already holds, with the same entry and version, is reused
  without a catalog call. Anything else is fetched, so sources a client sends are ignored.
- **The budget.** All modules' sources together are within 256 KiB (`MAX_INLINE_SOURCE_BYTES`), separate from the
  task's own source. A Spark task's code and modules travel inline together, so for Spark they share booth-spark's
  limit (248 KiB after the prelude's allowance).
- **Names.** A module name must be importable (not a keyword) and shadow nothing: not the standard library
  (`sys.stdlib_module_names`), not `task`, not `pyspark`, not `booth*`. Names must be unique, at most 20 modules,
  and Python entries only.
- **Import order.** `sys.path` is the task's folder, then `modules/`, then the task's own packages. Modules can
  import each other.
- **Spark.** The modules are embedded in the prelude, written to a temp folder in the driver, put on the driver's
  path, and shipped to the executors with `addPyFile`, so a UDF can import them too.
- **The run request carries `modules` only when a task has some.** That is runner protocol revision 3, so an
  operator image built before this release is refused by PR 3's check.

