# Design: the Spark runner and service-account owners (ADR 0119, ADR 0120)

Status: **accepted with rulings, 2026-10-16** (booth-architecture ADR 0119 amendment 3, ADR 0120 addendum 2). The
rulings below override the proposal where they differ; the sections they change are marked.

## Rulings (2026-10-16)

1. **Trust and subject.** Spark trusts core's signed `booth_module` claim (which names the minting module), not a
   subject prefix, so ask C5 needs no new claim. Pipeline still moves its subject to `pipeline:<ws>:<runId>`, as a
   naming convention and a second check. *Built.*
2. **Owner semantics kept** (section 9 item 2): the owner is whoever first saved a schedule and never changes on
   its own; a manual run acts as whoever pressed Run now. **Take over** is added as section 7 describes. *Built.*
3. **A scheduled run mints once at its start**, whatever its tasks need, and fails whole if core refuses, for person
   owners too (section 9 item 4).
4. **Spark driver log lines are shown to editors and owners only**; viewers see the state lines and the chip
   (section 9 item 5). Base task logs are unchanged; their residual (a base task can print its own token, which a
   viewer can read) is recorded as an open item.
5. **Refusal messages** (section 6 changes): core gives a code only for `service_account_disabled` and
   `service_account_not_found`. Every person cause, and module-not-entitled, stays one indistinguishable 403, so
   the person rows of section 6's table collapse into one corrected message pointing to Take over or a service
   account. *Built.*
6. **No usage registry** (section 7 and ask C4 change): no usage `PUT`/`DELETE`, no reconciliation, no
   delete-in-use enforcement by core; an account must be disabled before it can be deleted. Assigning is: authorize
   (a workspace owner), store. Pipeline shows which pipelines run as each account in its own UI. *Built:*
   `GET /api/service-accounts/uses`, and "Service accounts in use" on the pipelines list.
7. **Spark** adds the log cursor (S2) and accepts any token with the submission's `sub` (S3); the dry run (S4) is
   deferred.

The service-account picker waits for core's account-list route; until then an owner can assign an account through
`PUT /api/pipelines/{id}/owner`.

Inputs:
- booth-architecture `decisions/0119` (with its service-account amendment) and `decisions/0120`, both accepted
  2026-10-16;
- `agent-briefs/pipeline.md`, the 2026-10-16 entries for both;
- this repo's `docs/decisions/0006` (the seam left for Spark);
- booth-spark's published `/v1` interface (`internal/api/openapi/v1.yaml`, design-v0 section 6 and its step 3
  and step 5 as-built notes) and its operations doc (the SeaweedFS empty-folder note).

Section 9 lists where the ADRs and this repo disagree, each with a recommendation. Section 10 lists what I need
from booth-spark and booth-core.

## 1. What changes, in one paragraph

A task can say `runner: "spark"`. The pipeline backend mints the run's workload token, as for any task with
platform access; the minting credential never leaves the backend (ADR 0057). The task still gets its own Job
pod (ADR 0096), but that pod runs no user code. It runs a module-owned Spark client, which:
- submits the task's snapshotted PySpark code to booth-spark through core's gateway (ADR 0059), as the run;
- polls the application until it ends;
- copies its state and driver log into the task's log;
- stops the application if the task is cancelled or times out.

A pipeline's `owner` may also be a service account, `svc:<ws>:<name>`. Only a workspace owner can assign one.
Core resolves the account's role at every mint, and a disabled account fails the run at once with a clear
reason.

## 2. The runner contract

**Registration.** `SparkRunner` is registered beside `base` in `RunnerRegistry`, as 0006 planned. There is no
spec or API shape change beyond the task fields in section 3.

**Availability** is decided live, per workspace, never assumed (ADR 0006):
- **Not available** when `core.url` is empty, or the deployment has no minting credential. The reason names
  the setting.
- **Otherwise** `GET /api/runners` asks `GET {core}/modules/spark/v1/limits` with the caller's own token, and
  caches the answer for 60 s per workspace:
  - 200 means available;
  - the gateway's 404 means "booth-spark is not installed";
  - any other answer means unavailable, with that reason.

  `limits` is also what the builder uses to bound memory and executor inputs.
- **At save, a task naming `spark`** is refused (422) only if Spark is unavailable at that moment. The run
  checks again, and Spark itself has the last word.

**Execution**, per attempt (`runners/spark.py`, a thin layer over `KubernetesJobRunner`):
1. The engine mints the run's token. A Spark task always mints: `platformAccess` is forced on, and the
   builder shows it locked on, because a Spark run is submitted as the run. A refused mint fails the attempt,
   with the reason from section 6.
2. If an earlier attempt of this task left a Spark application, the backend makes sure it has ended before
   anything else (section 5).
3. The backend creates the task Job from the same chart template, with the invocation saying `runner: spark`
   plus the task's Spark options (section 3).
4. In the pod, the runner service starts `booth_pipeline.runners._spark_client` (module code, standard
   library only) instead of the Python or SQL harness. It:
   - builds `main.inlinePython` (section 3);
   - sends `POST /modules/spark/v1/applications` with the run's token, `X-Workspace` and
     `Idempotency-Key: <runId>/<taskKey>/<attempt>`, so a retried request inside one attempt can't start
     two runs;
   - polls `GET /v1/applications/{id}` every 5 s and the driver log (section 4);
   - re-reads its token file on every call, as the Python harness does, so the backend's token refreshes
     (`PUT …/token`) keep it valid for long runs.
5. **Admission:** a 429 `at_capacity` is waited out. The client retries every 30 s, logging each wait, up to
   `runner.spark.admissionWaitSeconds` (default 900). It then fails the attempt with Spark's own message. The
   wait doesn't count as an attempt.
6. **How it ends:**
   - `succeeded`: the task's output is read from the log (section 3);
   - `failed`: the task fails with `spark application <id> failed: <reason>`;
   - `stopped`: if the pipeline cancelled it, the task is cancelled; otherwise it fails as
     `spark application <id> was stopped: <reason>`.
7. **Cancellation and timeout.** On cancellation (the stream is hung up, then the Job is deleted) or the task
   timeout, the client calls `POST /v1/applications/{id}/stop` before exiting. The pod gets
   `terminationGracePeriodSeconds: 30` for that call. The backend also stops it itself, with a freshly minted
   token for the same run subject (section 5). Spark's `maxDuration` is set to the task's `timeoutSeconds`, so
   Spark ends the run even if both stops fail.

**Network.** The task pod already reaches core's gateway when `core.url` is set (the task NetworkPolicy's
`core` rule), so nothing is added. The pod's requests are small (the client is a poller): the chart gets
`runner.spark.resources` (default 10m CPU and 64Mi memory), separate from `runner.resources`.

**Concurrency.** A Spark task holds one `runner.maxConcurrent` slot while it waits on Spark, which is cheap. The
cluster-wide limit is Spark's admission, not ours.

## 3. How a task names its Spark script and options

**The code** comes from the catalog or from booth-storage, snapshotted at save like every task's code
(ADR 0063).
- It must be **Python (PySpark)**. A SQL or other-language task with `runner: spark` is refused at save. SQL on
  Spark (wrapping `spark.sql`) is a later addition, not v0.
- It is sent to Spark as `main.inlinePython`, never as `main.python {backendId, path}`. Spark reads a storage
  file when the run starts, so a run would execute whatever the file holds that day, not what the task
  version saved. The snapshot rule would break.
- **JARs are not supported** in v0: a snapshot is text, and catalog entries are text.

**The script's contract** mirrors the base runner's. The client wraps the source in a generated prelude, so
Spark's 256 KiB limit covers prelude, source and inputs. The prelude:
- creates `spark = SparkSession.builder.getOrCreate()` and a `ctx` with `params` (the node's merged params,
  ADR 0078), `inputs` (upstream outputs), `run_id`, `task_key`, `attempt`, `log` and `spark`;
- `exec`s the saved source compiled as `task.py`, so tracebacks show the user's own line numbers;
- if the script defines `run(ctx)`, calls it. Its JSON return value, at most 64 KiB, is printed as the last
  line of the driver log as `BOOTH-PIPELINE-OUTPUT <base64>`. A plain script returns nothing.

The client reads that line from the log Spark keeps when the run ends (its last 1 MiB). A run that succeeded
without the line, from a `run` that returned something, fails: "the Spark run succeeded but its output wasn't
found in its log". A source plus inputs over 256 KiB fails before submission, with a message to pass data
through the lakehouse instead.

`ctx.storage` and `ctx.catalog` don't exist inside Spark: the driver's pod is Spark's, not ours. Data goes
through `dataAccess`:
- `ctx.inputs` keeps small metadata passed between tasks;
- the lakehouse and the database are the data plane.

**Options** are a new `TaskConfig.spark`, required when `runner == "spark"` and refused otherwise:

```yaml
runner: spark
timeoutSeconds: 3600            # also the Spark run's maxDuration
spark:
  resources:
    driver: {memory: 1g}
    executors: {min: 1, max: 2, memory: 2g}
  conf: {spark.sql.shuffle.partitions: "8"}   # passed through; Spark's allowlist decides (400 names a refused key)
  args: ["--season", "2026"]                  # sys.argv[1:] for a plain script
  dataAccess:
    database: false
    lakehouse: true
    storage: [{backendId: seaweedfs-spark, path: nfl/raw, access: readwrite}]
```

**Checks at save** (pure, as for any task):
- **Shape and bounds:** memory matches Spark's pattern, at most 64 args, at most 4 storage locations, `access`
  is `read` or `readwrite`;
- **No Python requirements:** they install into the task pod, which isn't where the code runs. They are
  refused, naming the field. Spark-side packages are not in v0;
- **The 256 KiB code limit;**
- **What is left to Spark:** the conf allowlist and the limits per caller. The builder bounds its inputs with
  `/v1/limits`, and anything Spark refuses fails the attempt with Spark's 400 or 422 message.

A save-time dry run would catch these earlier. That is ask S4 in section 10, not a blocker.

**Export:** the YAML export carries `spark` inside `task.config`, as it does every config field.

## 4. Spark state and logs in the run view

**System lines in the task's log:**

```
spark: submitted application 7f3c… (driver 1g, 1–2 executors × 2g; data: lakehouse, storage seaweedfs-spark:nfl/raw rw)
spark: waiting for capacity (at_capacity: runs.maxRunning 2) — retrying in 30s
spark: pending → running
spark: running → succeeded (12m04s)
```

**The driver log** goes into the task's `stdout` stream, so the existing log viewer, its filters and
`maxLogLinesPerRun` apply. Spark's live log has only `?tail=N`, with no cursor. Until ask S2 lands:
- **while running**, the client fetches `tail=500` every poll and appends only the lines after the last line
  it already copied. If that line has scrolled out of the window, it says so (`spark: … lines skipped`), so
  nothing is silently lost;
- **when it ends**, it reconciles against the kept 1 MiB tail.

**The attempt's Spark record.** The client sends one structured event on the existing stream protocol, a new
event type `{"t": "external", "kind": "spark", "id": …}` next to `log` and `result`. The recorder stores it on
the task attempt as `external: {kind, id, state, reason, startedAt, finishedAt}` (a new JSON column) and updates
it as the state changes. The run view shows it as a chip on the task, `Spark · running · 7f3c…`. The chip links
to the booth-spark module's own run page (`/spark/runs/<id>`), where status shows for any member and logs only
for owners and operators. There is **no Spark UI link** for these runs (ADR 0119 ruling 3).

**A failed run's reason** is the same text as the attempt's failure message, so the run header shows Spark's
own reason, not a bare "failed".

**Who can read the driver log here** is a real choice; see section 9 item 5.

## 5. Retries, and `errorifexists` on SeaweedFS

**What can go wrong:**
- **Two applications writing at once.** If an attempt's pod dies but its Spark application keeps running, a
  retry would start a second application writing the same output.
- **A spurious "path already exists".** A Spark job that fails mid-write leaves its output directory emptied
  (the committer deletes `_temporary`). On S3 that path is then gone. On SeaweedFS the emptied folder lingers
  for about 2 minutes (`s3.empty_folder_cleanup_delay`), so a retry within that window that writes with
  `errorifexists` fails with "path already exists", although nothing was written.

**What the runner does:**
1. **One application at a time per task.** Before attempt N+1 (and on cancel and on stale-run recovery), the
   backend looks up attempt N's recorded application. It mints a token for the same run subject, as the
   submitter, and calls `GET /v1/applications/{id}`. If the application is still live, it calls `stop` and
   waits up to 60 s for it to end. If it is still live after that, the task fails without retrying: "the
   previous Spark application <id> is still running". It never starts a second one.
2. **A retry delay floor for Spark tasks.** When the previous attempt's application reached `running`, the next
   attempt waits at least `runner.spark.retryMinDelaySeconds`, default 150, above SeaweedFS's 120 s default.
   The task's own retry delay still applies if it is longer. Failures before `running` (a refused mint, a 400,
   a 422, admission) get no floor. The builder's retry help says so for Spark tasks.
3. **Idempotency-Key per attempt**, so a resend inside one attempt never makes two runs, while a retry is a new
   run.
4. **What is documented, not hidden.** `errorifexists` and retries conflict by nature. If an attempt committed
   its output and then failed later, every retry fails, correctly, rather than overwrite it. A task meant to be
   retried should write with `overwrite` (dynamic partition overwrite for partitioned tables) or an Iceberg
   `MERGE`/`INSERT OVERWRITE`. The floor only removes the SeaweedFS-specific false failure. The same 2-minute
   window applies to a person who deletes an output by hand and reruns straight away. The README says so.

## 6. A refused mint, said plainly

**Where a refusal is caught:**
- **Before the task starts:** the engine's existing `MintRefused` path already fails the task without retrying
  and records the reason on the run.
- **Mid-run**, on the backend's token refresh: the task keeps its current token until it expires (at most 10
  minutes). Spark's own re-mint for the Spark run is refused by core too, so Spark ends the run, and the task
  fails with Spark's reason.

**What changes is the message.** Core answers every refusal with the same 403 today, so pipeline can only
guess at the cause. Given core's refusal codes (ask C2), the run view says:

| Core's code | The task's message |
|---|---|
| owner not seen | the pipeline's owner, <name>, hasn't signed in to Booth in the last <ownerMaxAge>; nothing can run as them until they do, or a workspace owner makes a service account the owner |
| owner no longer a member, or demoted below what the run needs | the pipeline's owner, <name>, no longer has <role> access to this workspace |
| service account disabled | the pipeline's owner, service account `svc:acme:nfl-weekly`, is disabled; a workspace owner can enable it or choose another owner |
| service account deleted | the pipeline's owner, service account `svc:acme:nfl-weekly`, no longer exists; a workspace owner can choose another owner |
| module not entitled | booth-pipeline isn't entitled to mint tokens in this workspace (an operator setting) |

**Without codes** (core declines to tell person-owner causes apart, for the anti-probing reason
`workload.py` records), the person cases keep today's single text, corrected. Its "save the job to take it
over" advice doesn't work: the owner never changes once set. The new advice is to use **Take over** (section 7).
The service-account cases still need their own codes, since a member can see the accounts anyway.

## 7. Service-account owners (ADR 0120)

**Storage.** `Pipeline.owner_sub` (the field and its column stay) may now hold `svc:<ws>:<name>`. The `<ws>`
must equal the pipeline's own workspace. Stored beside it:
- `owner_kind`: `person` or `service`;
- `owner_name`: a display name taken when assigned, because the person directory is core's (ADR 0047).

**API.** `GET /pipelines/{id}` adds `owner: {kind, id?, name, you}`:
- for a service account, `id` is the account id;
- for a person, `id` is withheld and `you` says whether it is the caller;
- `hasOwner` stays.

Ownership changes through one call, `PUT /pipelines/{id}/owner {owner: "me" | "svc:<ws>:<name>"}`:

| Change | Who may make it |
|---|---|
| to a service account | workspace owners only (ADR 0120 point 3); 403 otherwise |
| to themselves ("Take over"), from a person owner | an editor or an owner |
| to themselves, from a service-account owner | workspace owners only, so an editor can't remove what an owner set up |
| to another person | nobody, in v0. A person can only take a pipeline over, never hand it to someone else |

Saving a schedule still sets the owner only when there is none, as today.

*(Superseded by ruling 6: assigning is authorize, then store. The usage registration below is not built.)*

**Assigning an account**, in this order, so a pipeline never names an account core doesn't know is in use:
1. The backend calls core's usage API with the caller's own token (ask C4):
   `PUT /api/workspaces/{ws}/service-accounts/{name}/uses/pipeline/{pipelineId}`. Core checks that the caller
   is a workspace owner, and that the account exists and is enabled.
2. Only on success does the backend store the owner.
3. The previous owner, if it was an account, is released with `DELETE …/uses/pipeline/{pipelineId}`.

A crash between steps leaves an extra "use", which blocks a delete. It never leaves an owner core doesn't know
about.

*(Superseded by ruling 6: core enforces nothing about use; an account is disabled before it is deleted.)*

**Delete-in-use is enforced by core**, from those registrations. Pipeline also:
- releases a use when a pipeline is deleted, or its owner changes;
- **reconciles** on startup and hourly, using its module credential (ask C4): it sends its full list of
  service-account owners, and core replaces pipeline's registrations with it. Drift from a crash or a restored
  backup heals by itself.

Pipeline doesn't enforce delete-in-use itself: the account is core's record.

**What a service-account owner changes at run time:**
- **The mint is unchanged.** `owner` is already sent to core; core resolves the account's role, live, with no
  `ownerMaxAge` window. The effective role is the lesser of `roleCeiling` and the account's role.
- **Manual runs** stay the clicker's (`run_now` uses the caller's identity, as today). The run view says who a
  run acts as: "Ran as service account `svc:acme:nfl-weekly`", or "Ran as Dougall (Run now)".
- **Spark:** `booth_owner` carries the account id, and Spark's data-access mint resolves it the same way (the
  ADR 0119 amendment).

**A disabled or deleted account:**
- the next mint is refused with the codes in section 6, and the run fails with that reason;
- a running task keeps its token until it expires (at most 10 minutes), and a running Spark application ends
  at Spark's next re-mint;
- schedules keep firing, and each run fails visibly at once. They are not paused silently;
- the pipeline list and the pipeline's header show "Owner disabled" when core reports it (ask C3). See section
  9 item 4 for tasks that need no token.

**The picker** is in the pipeline's Schedule tab, as "Runs as":

| Viewer | What they see |
|---|---|
| workspace owner | a select listing "Me (<name>)" and the workspace's service accounts from core, with role and description. Disabled accounts are listed but greyed out, "(disabled)". It links to the shell's workspace settings to create an account |
| editor | the current owner read-only, plus **Take over** when the owner is a person |
| viewer | the owner, read-only |

The picker needs pipeline-ui **1.3.0**, and the Spark options panel ships in the same release.

## 8. Builder and run-view changes (pipeline-ui 1.3.0)

**Task settings:**
- choosing "Spark" in "Runs on" (enabled when available) shows a Spark panel: driver memory, executors (min,
  max, memory), conf as key/value rows, args, and data access (database, lakehouse, up to 4 storage locations
  with the existing storage picker and read/readwrite);
- limits come from `/v1/limits`;
- platform access shows locked on, with the reason;
- "Python packages" is hidden;
- the retry help shows the 150 s floor.

**Run view:** the Spark chip per task attempt, with a link to the Spark module's run page, and the
"ran as" line.

**Schedule tab:** the "Runs as" picker from section 7.

**Release:** pipeline-ui 1.3.0, a minor release because every change only adds something, plus booth-design's
pin bump (theirs). The backend ships as booth-pipeline 0.2.0: the minor version moves for a new runner and a new
ownership model.

## 9. Where the ADRs and this repo disagree

1. **The token's subject.** ADR 0119 decision 2 has Spark accept "`sub` is `pipeline:<id>`". Pipeline mints
   `sub = run:<runId>` (`runs.py`). **Recommendation:** pipeline moves to `pipeline:<ws>:<runId>`, the fleet's
   `<kind>:<ws>:<id>` form (Spark's `spark:<ws>:<runId>`, Streamlit's `streamlit:<ws>:<appId>`). Spark then
   checks the `pipeline:` prefix. That check means something only if core binds the kind to the minting module
   (ask C5). Otherwise any minting module could name itself `pipeline:` and get unattended data access.
2. **Who the owner is.** ADR 0119's context says the owner is "the creator, or whoever last saved a schedule".
   In this repo the owner is whoever **first** saved a schedule, and it never changes afterwards. A manual run
   acts as the person who pressed Run now, so their `sub` is `booth_owner` for that run's Spark data access.
   **Recommendation:** keep both, add Take over (section 7), and correct ADR 0119's wording.
3. **The refusal message's advice is wrong today.** It tells people to "save the job to take it over", which
   does nothing once an owner is set. It is fixed by Take over and the new messages (section 6).
4. **A disabled owner doesn't stop tasks that need no token.** A task without platform access never mints, so a
   scheduled run of a pipeline whose account is disabled (or whose person owner lapsed) still runs those tasks.
   They run without access, but they still run code. **Recommendation:** a scheduled run mints once at run
   start, whatever its tasks need. If core refuses, the whole run fails with the reason before any task starts.
   This applies to person owners too, which changes today's behaviour (needs your yes).
5. **Driver logs and who can read them.** Spark lets only the submitter, workspace owners and operators read a
   run's log, because code can print its own credentials. A data run's credentials include S3 lease keys
   (valid 15 minutes or more) and the editor-capped token. Pipeline lets **viewers** read every task log. So
   copying the driver log in would let a viewer read whatever the Spark code prints.
   - The same is already true of base tasks: their code can print their own token to stdout, which viewers can
     read. That is an existing residual, and I hadn't stated it before.
   - **Recommendation:** show Spark driver log lines to editors and owners only (viewers see the state lines
     and the chip). Separately, decide whether base task stdout and stderr should get the same rule. Editors
     already control the code, so they gain nothing new.
   - Your call on both.

## 10. What I need

**From booth-spark:**

| | Ask |
|---|---|
| S1 (blocking) | ADR 0119 decision 2: accept a workload-token submission with `dataAccess` when the token has `booth_owner`, minting the data token with `owner = booth_owner` and `roleCeiling = min(editor, the token's role)`, plus the five tests in decision 5. Until then the runner works for runs without `dataAccess`, and a run with it fails with Spark's 422 shown as is. |
| S2 (wanted) | a log cursor: `GET /v1/applications/{id}/logs?from=<byte offset>`, returning the next offset (for example in `X-Log-Offset`), live and after the run ends, so the task log is a faithful copy, not a tail-and-dedupe. |
| S3 (confirm) | that `stop`, `GET` and `logs` accept any token with the same `sub` as the submission's, so the backend's fresh token for the run can stop an orphaned application; and that `Idempotency-Key` is scoped to that `sub`. |
| S4 (optional) | a validation-only submission (`POST /v1/applications?dryRun=true`, admission and conf/limits checks, no run), so a task's Spark options can be checked at save. |
| S5 (confirm) | the subject prefix check of section 9 item 1, once core answers C5. |

**From booth-core:**

| | Ask |
|---|---|
| C1 (blocking) | the `booth_owner` claim on workload tokens (ADR 0119 decision 1), carrying a person's `sub` or `svc:<ws>:<name>`. Pipeline's mint call already sends `owner`; nothing changes on our side. |
| C2 (blocking for the messages) | a machine-readable code on a refused mint: `{error, code}`, with `owner_inactive` (and the window), `owner_not_member`, `owner_role_insufficient`, `service_account_disabled`, `service_account_not_found`, `module_not_entitled`. At least the two service-account codes, if the person codes must stay indistinguishable. |
| C3 | service accounts readable through the gateway: the list (with role, description and disabled) for workspace owners, for the picker, and a single account's `{name, role, disabled}` for any member, for the "Owner disabled" badge. If members can't read it, the badge shows only after a failed run. |
| C4 (blocking for delete-in-use) | usage registration: `PUT`/`DELETE /api/workspaces/{ws}/service-accounts/{name}/uses/{module}/{resourceId}` with the caller's token (core checks owner role and an enabled account), and a module-credential `PUT /api/internal/service-account-uses/{module}` that replaces a module's whole set, for reconciliation. Core refuses deleting an account that has uses, and lists them. |
| C5 | whether core binds a minted subject's kind to the minting module (pipeline's credential may mint only `pipeline:…`). If not, a claim naming the minting module (for example `booth_minted_by`), so Spark's check rests on something core signed. |

## 11. How it will be proved

**Unit tests**, against a fake Spark API and a fake core:
- the contract in section 2;
- the prelude and output marker;
- the 256 KiB and option checks;
- the log copy, including skipped-line reporting;
- admission waits;
- cancel calls `stop`;
- before a retry, the previous application is stopped and waited for; the floor applies only after `running`;
- each refusal code's message;
- the owner rules table;
- the usage-registration order and reconciliation.

**Integration (kind, Calico)** with a stand-in Spark API at booth-spark's gateway path, as
`fake-core-broker.yaml` stands in for core today. Real Spark doesn't fit the CI node, as booth-spark's own notes
say. It covers:
- a scheduled run's Spark task end to end: submit, states, logs, output to a downstream task;
- cancel stops the application;
- a killed task pod's application is stopped before the retry;
- the retry floor;
- a refused mint shows its message in run status;
- a service-account owner: assignment by an owner, refused for an editor, a disabled account fails the run
  with its message, and delete-in-use is refused by the stand-in core.

**The real gate** (ADR 0119 gate 3, ADR 0120 gates) runs on the real stack, with booth-spark, core and SeaweedFS:
a scheduled pipeline owned by a service account runs a Spark task that writes a lakehouse table, and an owner
too old (or a disabled account) shows its message. That runs once S1, C1, C2 and C4 have landed, on the homelab
or in booth-e2e. I will report which.

**Build order** (after you accept this note and the asks are answered): owner model and API (section 7) →
Spark runner backend (sections 2, 5, 6) → task-pod client → builder and run view → Integration → real-stack
gate. The owner work doesn't depend on Spark and can merge first, once C4 exists.

## 12. As built (booth-pipeline 0.2.0, pipeline-ui 1.3.0)

Built in three PRs: ownership and Take over (#16), mint at a scheduled run's start (#17), the Spark runner. Where
the build differs from the sections above, or adds to them:

- **How the pod talks to the backend.** No new stream event type: the task pod's Spark client writes its events
  (`submitted`, `state`, `waiting`) as marked lines on stderr, and `runners/spark.py` turns them into system lines
  and the attempt's record, so they never reach the log as text. The driver log arrives on stdout and is stored as
  the `spark` stream. The pod runs only module code, so nothing user-written can forge an event.
- **The attempt's record** is `task_runs.external` (migration 0007): `{kind: spark, id, state, reason}`, shown as
  the chip in the run view, linking to `/spark/runs/<id>`.
- **A Spark task's pod gets no credential sidecar**: it needs the run's token, not a database or S3 lease (Spark's
  pods get those).
- **Failure reasons** come from the client through `failure.txt` in the task's working directory (any harness may
  write it; the runner service uses it instead of "exited with code N").
- **The log copy** uses booth-spark's cursor (`?from=`, `X-Log-Offset`) while the run lives. Against a booth-spark
  without it, the kept log is copied once when the run ends (no tail-and-dedupe).
- **The retry floor** is applied by the engine before the retry, in full, on top of the task's own retry delay,
  and only when the application reached `running`.
- **Code size:** the saved code is limited to 248 KiB at save; the prelude and the inputs make up the rest of
  Spark's 256 KiB, checked when the attempt starts.
- **The stand-in Spark API** (`tests/integration/fixtures/spark/`) runs the submitted code for real against a stub
  `pyspark`, and is shared by the unit tests and Integration. Integration drives scheduled runs through the API
  pod's own service (store, engine, Job runner, Spark runner), not over HTTP, because no identity provider is in
  that cluster. The ADR 0119 gate 3 run on the real stack (real core, real booth-spark, SeaweedFS) is still to do.
- **Waiting on core's routes:** the service-account picker (the account list) and the "Owner disabled" badge
  (a single account's state). Until then a workspace owner assigns an account with `PUT /api/pipelines/{id}/owner`.

