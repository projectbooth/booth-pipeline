# 0019: Adopting booth-core's credential sidecar (ADR 0095) — postgres and s3

Status:
- **postgres mode built**, verified on a real CNI by `.github/workflows/integration.yml` against the
  real digest-pinned sidecar.
- **The `boothStorage.url` egress rule built.**
- **s3 mode built (2026-10-05) to ADR 0095's third amendment**, with the scope resolved per task from
  booth-lakehouse. It is verified on a real CNI against the same sidecar, but it is **only usable end
  to end once the chart pins booth-core's corrected sidecar**. The pinned eb24bb3 refuses
  booth-storage's real s3 credential and writes no endpoint/region file (below).

## What was built (postgres)

ADR 0096 gives every task its own pod, so ADR 0095's original shape applies: one sidecar per pod,
with no per-task processes. Gated by ADR 0092's existing `boothDatabase.url` **and** `core.url` (the
sidecar calls core's broker; with no core there is only the egress rule, as before).

- **A native sidecar**: an init container with `restartPolicy: Always` (Kubernetes ≥ 1.29). It
  starts before the task and is stopped when the task's container exits. A plain second container
  would keep a Job's pod running after its task ends.
- **Image**: `ghcr.io/projectbooth/credential-sidecar@sha256:05e96332…` (booth-core@eb24bb3, publish run
  37012736996). The chart refuses a tag, and so does `KubernetesJobRunner` at startup. Its args are
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

**Found while building it — today's pinned sidecar can't take booth-storage's real s3
credential.** eb24bb3's `S3Credential` has only the three key fields, and the sidecar decodes the
broker's credential with `DisallowUnknownFields`. booth-storage's real response also carries
`endpoint`, `region`, `bucket`, `keyPrefix` and `pathStyle`. So against real booth-storage the
pinned sidecar refuses every s3 lease as unparseable and writes **no** file at all, which is worse
than "keys only". booth-core's planned change (adding the location fields to `S3Credential`)
removes this as a side effect. Its tests should use booth-storage's real response shape so this
can't recur.

**What is verified now, and what waits for booth-core.** The Integration workflow runs the real
pinned sidecar against a stand-in broker. That broker issues keys-only s3 credentials (fixture
`S3_CREDENTIAL_SHAPE=keys`) because that's all eb24bb3 accepts. It shows, on a real CNI:
- blocked before `boothStorage.url` and reachable after;
- keys written for exactly the lease scoped to the workspace's warehouse;
- no s3 sidecar for a workspace without a warehouse, or for a task without access;
- the file rewritten with new keys while a task runs, never seen half-written.

When booth-core publishes the corrected sidecar, two changes make it end to end:
- bump `credentialSidecar.image` to the new digest;
- set the fixture's `S3_CREDENTIAL_SHAPE=full`, and turn the step's "endpoint/region file written"
  report into an assertion on `credentials.config`.

## Known limit, inherited (booth-notebooks' Finding 1)

The sidecar contract says a connection opened on an earlier lease "is left alone until it closes
naturally". booth-database's reaper, however, terminates sessions when their lease expires. Through
real booth-database, a connection therefore lives at most one lease (an hour by default), whatever
renewal does. The forced-rotation check here proves what the sidecar promises: a connection held
open survives the sidecar's renewal onto a new lease, and new connections never fail meanwhile. Its
stand-in provider doesn't reap, so the check does not cover booth-database's expiry behavior. A
pipeline task that runs longer than a lease and holds one connection throughout would see it
dropped. That is a coordinator question between booth-core's contract and booth-database, not
something this module can fix.
