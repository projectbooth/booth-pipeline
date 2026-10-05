# 0019: Adopting booth-core's credential sidecar (ADR 0095) — postgres built, s3 held back

Status: **postgres mode built**, verified on a real CNI by `.github/workflows/integration.yml` against
the real digest-pinned sidecar. **The `boothStorage.url` egress rule built.** **The s3-mode sidecar
not built**: two gaps outside this repo, the same ones booth-notebooks recorded in its own 0008, still
unruled (below).

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

## Not built: the s3-mode sidecar

These are the same gaps booth-notebooks' 0008 records. None has been ruled on as of 2026-10-05, and
booth-core is still at eb24bb3.

1. **The sidecar writes keys only.** `--kind=s3` writes `aws_access_key_id`, the secret and the
   session token. booth-storage's s3 grant also carries the location (`MintedCredential`: endpoint,
   bucket, key prefix, region, path style). With an in-cluster MinIO, PyIceberg and DuckDB can do
   nothing with keys alone. Needs booth-core: write the endpoint too (e.g. an AWS config file beside
   the credentials), or expose the grant's location some other way.
2. **The scope is not derivable here.** booth-storage's s3 scope is `{backendId, path}`: the
   workspace's lakehouse warehouse location. A pipeline task knows its workspace, not where
   booth-lakehouse put that workspace's warehouse. Needs a ruling on who supplies it: a lookup
   against booth-lakehouse (a new module-to-module dependency nobody has specified), or a warehouse
   location that can be derived from the workspace.

A third gap, the missing gate, is now answered by `boothStorage.url`. Building the container
anyway would ship a sidecar that cannot be configured correctly. Once the two gaps are ruled on,
the s3 sidecar is a second native init container next to the postgres one, `--credentials-file` on
the same private volume, personalized per task the same way.

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
