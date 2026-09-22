# 0002: Pipeline tasks are user code — the base runner is not a security boundary

Status: **RESOLVED by ADR 0057 (option 1 below) and built — see [0007](0007-workload-identity-adoption.md).** User code
now runs in a separate, credential-less runner pod; the in-pod subprocess runner remains only for local dev / single-tenant
installs and is refused whenever a workload-minting credential is present. Kept below as the record of the problem.
(Original status: OPEN — v0 first pass shipped the weakest option behind a seam.)

## The problem

A pipeline task is arbitrary Python written by any user with `editor` or `owner` in a workspace. The base runner
executes it as a **subprocess of the module pod**. That subprocess is a different *process* (state isolation, hard
timeout, cancel by killing the tree, no shared memory) but the same OS user in the same container. So task code can:

1. Read the module's database credential (a mounted/env Secret — `/proc/<ppid>/environ` and the mount are readable by
   the same uid). That role owns **every workspace's** pipelines, jobs, runs and logs. **An editor in workspace A can
   read and rewrite workspace B's data.** This breaks multi-workspace tenancy (ADR 0008) for this module.
2. Use the pod's network identity (cluster-internal services, the gateway, other modules' pods).
3. Exhaust the pod's CPU/memory/disk (the chart bounds `/tmp`; there is no per-task cgroup).

## What v0 does anyway (mitigations, not a fix)

- Task subprocess environment is **scrubbed**: none of the module's variables (DSN, OIDC, core URL) are inherited;
  `BOOTH_PIPELINE_RUNNER_ENV_PASSTHROUGH` opts names back in. Tested.
- `automountServiceAccountToken: false`, no RBAC, read-only root filesystem, non-root, all capabilities dropped
  (chart contract tests + a test that runs tasks inside that lockdown).
- Editors/owners only can save or run (viewers can read logs) — mirroring ADR 0038/0048, no new permission model.

None of that stops item 1: the DSN is still readable from a mounted Secret or `/proc`.

## Options (recommendation first)

1. **A separate runner Deployment with no credentials** (recommended). The API/scheduler pod (has the DB) dispatches
   a task snapshot to a runner pod over an authenticated internal call and streams logs back; the runner has no
   Secret, no DB, no service-account token, and can be given its own NetworkPolicy and resource limits. Fits the
   existing `Runner` seam (`runners/base.py`): a `RemoteRunner` replaces `SubprocessRunner`; nothing above it changes.
2. **A Kubernetes Job per task** in a dedicated namespace/service account. Strongest isolation, cold-start cost per
   task, and needs RBAC the module currently (rightly) does not have; the same seam applies.
3. **Accept for v0 in single-tenant installs only**, documenting that pipeline write access = code execution as the
   module. Honest, but the platform is multi-workspace from v0.

## Ask

Coordinator: pick 1, 2 or 3 (or say v0 may ship as 3 with a documented single-tenant warning). If 1 or 2, it is a
cross-cutting change (auth between pods, deployment topology) that probably deserves its own ADR, and the same answer
will be wanted by `booth-notebooks` (JupyterHub/KubeSpawner already isolates per user) so the two don't diverge.
