# 0019: Adopting booth-core's credential sidecar (ADR 0095) — postgres and s3

Status:
- **postgres mode built**, verified on a real CNI by `.github/workflows/integration.yml` against the
  real digest-pinned sidecar.
- **The `boothStorage.url` egress rule built.**
- **s3 mode built (2026-10-05) to ADR 0095's third amendment**, with the scope resolved per task from
  booth-lakehouse. Since 2026-10-06 the chart pins booth-core@8f0c6b4, which accepts booth-storage's
  real s3 credential and writes the endpoint/region/addressing-style file. Verified on a real CNI
  (below).

## What was built (postgres)

ADR 0096 gives every task its own pod, so ADR 0095's original shape applies: one sidecar per pod,
with no per-task processes. Gated by ADR 0092's existing `boothDatabase.url` **and** `core.url` (the
sidecar calls core's broker; with no core there is only the egress rule, as before).

- **A native sidecar**: an init container with `restartPolicy: Always` (Kubernetes ≥ 1.29). It
  starts before the task and is stopped when the task's container exits. A plain second container
  would keep a Job's pod running after its task ends.
- **Image**: `ghcr.io/projectbooth/credential-sidecar@sha256:6a0a795e…ce14` (booth-core@8f0c6b4, publish
  run 37464892322; earlier pins: eb24bb3, run 37012736996; 330a178 `decd3031…5865`, run 37378106842). The chart refuses a tag, and so does `KubernetesJobRunner` at startup. Its args are
  `--kind=postgres --listen=127.0.0.1:5432 --token-file=/var/run/booth-sidecar/token --core-url=…`.
  It is non-root, has a read-only root filesystem and drops all capabilities.
- **Per task, by `KubernetesJobRunner`**:
  - It adds `--workspace=<ws> --scope={"workspace":<ws>} --access=…`.
  - It adds `DATABASE_URL=postgresql://localhost:5432/bdb_ws_<hash>`, using booth-database's
    `naming.ForWorkspace` (the same derivation as booth-notebooks). The URL has no host and no
    credential.
  - `--access` follows the role core actually granted the task's token: `read` for a viewer,
    `readwrite` otherwise. The broker refuses `readwrite` to a viewer, and a refusal makes the
    sidecar exit.
- **A task without platform access gets no sidecar** and no `DATABASE_URL`. The sidecar
  authenticates *as the task*, so it has no identity to use; it could only crash-loop.
- **The token**:
  - The runner keeps the task's own platform token at `/var/run/booth-sidecar/token`. That is a
    memory-backed emptyDir, private to the task's pod; only the runner and the sidecar mount it.
  - It is refreshed by the same PUT that refreshes the task's own token file.
  - It is removed when the task ends.
  - This is only set in a one-task pod. In a shared process it would gather every task's token in
    one place.
- **Waiting for the lease**:
  - The contract says a task that starts before its sidecar has a lease "should wait, not fail". So
    before a task with access starts, the runner polls the sidecar's loopback `/healthz`, for up to
    `credentialSidecar.waitSeconds` (60).
  - After that the task starts anyway, with a note in its log. A task that never touches the
    database must not be blocked by a database outage. This follows booth-notebooks' reasoning.
  - Polling from inside the pod also avoids booth-notebooks' Finding 2: the kubelet cannot probe a
    loopback-only listener, so the sidecar has no readiness probe.
- **`DATABASE_URL` is checked before it reaches the task**: the runner service accepts only a
  credential-free loopback `postgresql://` URL, and passes nothing else from its environment to the
  task.

**Found on a real cluster:** the lease wait first ran before the runner wrote the task's token. The
sidecar waited for the token and the runner waited for the sidecar, so each task sat out the full
60s and then failed with "no credential lease obtained yet". Unit tests of each piece had passed;
only the real cluster showed the order. The wait now runs inside the subprocess runner, after the
token is written and before the task's process starts, and a unit test pins that order.

## `boothStorage.url` (ADR 0095 amendment)

`boothStorage.url` mirrors `boothDatabase.url`. It is off by default; when set, it adds one egress
rule to the operator's S3 backend pods on `runner.networkPolicy.egress.boothStorage.port` (9000 by
default). booth-storage ships no backend of its own (it is whatever MinIO or S3 the operator
configured), so there are no labels to default to. Its namespace and pod selectors are therefore
**required** when the URL is set: the chart refuses to render rather than guess, because an empty
selector would match far more than the backend. It opens a route only: no sidecar, no credential.

## s3 mode (ADR 0095, third amendment)

The two gaps this record first raised were ruled on 2026-10-05. The scope is resolved through
booth-lakehouse. The endpoint and region become booth-core's job: a second file next to the keys.

- **Gate**: `boothStorage.url` **and** `core.url`, the same pattern as postgres. A second native init
  container, `credential-sidecar-s3`, with these args:
  - `--kind=s3`
  - `--credentials-file=/var/run/booth-sidecar-s3/credentials`, on its own memory-backed volume:
    writable by the sidecar, read-only to the task, and not mounted by the postgres sidecar
  - `--health-listen=127.0.0.1:8081`
  - `--token-file` (the same task token) and `--core-url`
- **The task gets** `AWS_SHARED_CREDENTIALS_FILE=…/credentials` and
  `AWS_CONFIG_FILE=…/credentials.config`, the contract's two-file output and the standard variables
  every S3 client honors. They are paths, never credentials, and the runner service refuses anything
  outside the sidecar's directory.
- **The scope**:
  - `KubernetesJobRunner` asks booth-lakehouse's `GET /api/warehouse` through core's gateway
    (`<core.url>/modules/lakehouse/api/warehouse`). It sends the **task's own token** and
    `X-Workspace`, before creating the Job, and only for a task with platform access.
  - The sidecar gets `--scope={"backendId":…,"path":…}`, taken from the response and nothing else,
    plus the task's `--workspace` and `--access` (`read` for a viewer).
  - **A 404 (no warehouse yet) means no s3 sidecar and no AWS variables**, exactly as for a task
    without platform access, and with no log note: it is an ordinary state.
- **Any other lookup failure** (booth-lakehouse or core down, a 403, an unexpected body): the task
  still runs, without s3 credentials, with a note in its log. This is a judgment call, for the same
  reason the lease wait doesn't block: a task that never touches s3 must not fail because
  booth-lakehouse is down.
- **New runtime coupling, called out per the brief**: with `boothStorage.url` and `core.url` set,
  starting a task with platform access now calls booth-lakehouse, through core's gateway, from the
  API pod. That adds up to 10s on a task start if booth-lakehouse hangs. An install without
  `boothStorage.url` makes no such call.
- **Waiting for leases**: the runner now waits for every sidecar the task actually has. The Job
  carries the list in `BOOTH_RUNNER_SIDECAR_HEALTHZ`, set per task. The note on a timeout names which
  sidecar is still waiting.

**Found while building it — a port clash the contract's defaults would cause.** The s3 sidecar's
`--health-listen` defaults to `127.0.0.1:8080`, and the runner already listens on `0.0.0.0:8080` in
the same pod. The chart sets `:8081`, and `KubernetesJobRunner` refuses a template whose sidecar
health port equals the runner's.

**Found while building it — the first pinned sidecar couldn't take booth-storage's real s3
credential (fixed by the repin).** eb24bb3's `S3Credential` has only the three key fields, and the sidecar decodes the
broker's credential with `DisallowUnknownFields`. booth-storage's real response also carries
`endpoint`, `region`, `bucket`, `keyPrefix` and `pathStyle`. So against real booth-storage the
pinned sidecar refuses every s3 lease as unparseable and writes **no** file at all, which is worse
than "keys only". booth-core's planned change (adding the location fields to `S3Credential`)
removes this as a side effect. Its tests should use booth-storage's real response shape so this
can't recur.

**Verified, against the repinned sidecar (booth-core@8f0c6b4).** The Integration workflow runs the
real sidecar against a stand-in broker that issues s3 credentials in booth-storage's real shape. It
shows, on a real CNI:
- blocked before `boothStorage.url` and reachable after;
- keys written for exactly the lease scoped to the workspace's warehouse;
- `credentials.config` written alongside: `endpoint_url`, `region` and `addressing_style = path`;
- no s3 sidecar for a workspace without a warehouse, or for a task without access;
- the files rewritten with new keys while a task runs, never seen half-written.

## DuckDB: `ctx.s3`, and SQL tasks set up for free

DuckDB and pyarrow don't take `endpoint_url` from `AWS_CONFIG_FILE` on their own
(contracts/credential-sidecar.md, "Known limitation"), so against a self-hosted backend they would
go to real AWS. The task image ships DuckDB, which runs every SQL task, but not pyarrow or boto3.
The helper follows booth-notebooks' `booth.s3` (its `docs/decisions/0009`):

- **`runners/_s3.py`** (standard library only; it takes a DuckDB connection, never imports DuckDB)
  reads the sidecar's config file into a location (endpoint, region, addressing style). It builds a
  `CREATE SECRET … PROVIDER credential_chain, REFRESH auto` holding **only the location**. The keys
  stay with DuckDB's own AWS credential chain, which reads the sidecar's credentials file, so
  renewals reach it with no code here and no key ever enters SQL text.
  - `addressing_style` is accepted in **both** forms: nested under `s3 =`, which booth-core@8f0c6b4
    writes (ADR 0095, sixth amendment), and the top-level key 330a178 wrote. The nested one wins
    if both are present, since it is the one botocore honors.
  - `virtual` maps to DuckDB's `URL_STYLE 'vhost'`, `path` to `'path'`; `auto` or unstated leaves
    DuckDB's default.
- **Python tasks** get `ctx.s3.duckdb(con)` and `ctx.s3.location()`. Without s3 credentials they
  raise `PlatformError` with the fix, like `ctx.storage`.
- **SQL tasks** can't call a helper, so the SQL harness creates the secret itself whenever the task
  has s3 credentials, and `SELECT * FROM 's3://bucket/key'` just works. If that fails (no lease yet),
  the query still runs, and the task log says why `s3://` paths won't resolve.
- **The `httpfs` and `aws` extensions are baked into the image** (`/opt/duckdb-extensions`) and only
  LOADed. booth-notebooks installs them on first use, but a pipeline task pod has no internet by
  default, so `INSTALL` would fail in exactly the pods that need it. Measured: both load with
  `--network none` under the chart's lockdown. The image grows by about 73 MB.
- **Verified end to end in Integration** against a real MinIO (the platform's pinned test image, ADR
  0087/0091), with real per-lease MinIO users so a renewal is a real key rotation. A Python task
  writes Parquet with `ctx.s3`; a SQL task in its own pod, on its own lease, reads it back with no
  setup.

## Connection lifetime (ADR 0095, fifth amendment) — what a task author can rely on

Ruled 2026-10-06: booth-database's reaper stays strict, so a credential dies at its lease's expiry
(ADR 0080), and the sidecar contract is corrected to match (contracts/credential-sidecar.md,
"Connection lifetime"). For a pipeline task using `DATABASE_URL`:

- **A connection ends no later than its lease's expiry** (one hour today). Renewal doesn't extend
  it; renewal only means the *next* connection gets a fresh lease.
- **A connection is guaranteed at least the renewal margin** before it can be cut. The chart leaves
  `credentialSidecar.renewMarginSeconds` at 0 on purpose, so the chart never sets
  `RENEW_MARGIN_SECONDS`. With booth-core@8f0c6b4 (pinned), postgres mode then renews at **half the
  lease's lifetime**, so a connection is guaranteed about 30 minutes of a one-hour lease. Setting
  `renewMarginSeconds` replaces that with a fixed margin, which shortens the guarantee: don't, except
  in tests that need fast rotation (the Integration workflow sets 20s for exactly that).
- **So a task must not hold one database connection across a long idle gap or a multi-hour run, and
  should reconnect when a connection is dropped**: open a connection per unit of work, or use a pool
  that recycles connections (for example SQLAlchemy's `pool_pre_ping=True` with `pool_recycle`
  under 30 minutes).
- The forced-rotation check here proves the sidecar's side of this: a connection held open survives
  the sidecar's renewal onto a new lease, and new connections never fail meanwhile. Its stand-in
  provider doesn't reap, so it doesn't exercise booth-database's expiry itself.
- A task that genuinely needs one uninterrupted multi-hour connection is the case that would justify
  revisiting the reaper. It goes back to the coordinator, not worked around here.
