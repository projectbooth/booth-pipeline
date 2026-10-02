# 0018: Adopting ADR 0096 — design for `KubernetesJobRunner`

Status: **ratified 2026-10-02** as ADR 0096's amendment ("invocation/token/log channel, and the
NetworkPolicy boundary"), including the clarification below: the chart offers only Job-per-task;
`SubprocessRunner` stays as each Job pod's engine and the local-dev/unit-test path.

ADR 0096 fixes the decision (one Kubernetes Job, one pod, one task, closing the cross-workspace
token-theft hole) and first prescribed mechanisms for getting work in and out of the task pod.
Checked against this repo before building, two of those mechanisms didn't fit and one instruction
needed a clarification. This is the design that was proposed in response and ratified.

## What doesn't fit, with evidence

1. **The task can't travel by Secret/ConfigMap.** A task attempt needs its source (up to
   `MAX_TASK_SOURCE_BYTES` = **8 MiB**, `model.py`), params (64 KiB) and inputs — its upstream
   tasks' outputs, each up to **1 MiB** (`_harness.py` `MAX_OUTPUT_BYTES`), several for a fan-in.
   A Kubernetes Secret or ConfigMap is capped at **1 MiB**. ADR 0096 says nothing about how the
   invocation reaches the pod; the only built-in channels can't carry it.
2. **The result has no channel back under the prescribed log path.** Today the result (up to 1 MiB)
   returns on the runner's authenticated HTTP stream. With "follow logs via `pods/log`" the only
   way back is a sentinel line parsed out of a stream the task itself writes to, subject to the
   node's container-log rotation.
3. **The runner `NetworkPolicy` is the task-egress lockdown, not just the runner's.** It is the only
   thing giving task code "no route to the module's own database", DNS + booth-core's gateway only,
   internet closed by default (ADR 0070), and ADR 0092's opt-in booth-database rule. "Retire the
   runner's NetworkPolicy" can only mean *move it onto the task pods* — dropping it would open task
   egress completely.

## Proposed design: each Job pod is a one-task runner service

The repo already has a tested, authenticated protocol for "run one task somewhere else":
`runner_service.py` (the server: `POST /v1/run` streams NDJSON log lines and the result back,
`PUT …/token` swaps a running task's token) and `RemoteRunner` (the client, including the Linux
instant-cancel fix from `3845965`). The proposal: **each Job pod runs that service for exactly one
task, and the API/scheduler pod drives it with `RemoteRunner`.**

Per task attempt, `KubernetesJobRunner` (in the API/scheduler pod):

1. Waits for a slot under `runner.maxConcurrent` (an in-process cap on outstanding Jobs).
2. Creates a **Secret** holding a fresh random per-task runner bearer (never a platform token, never
   anything the task needs to read), then a **Job** (`backoffLimit: 0`, `restartPolicy: Never`,
   `activeDeadlineSeconds` = task timeout + margin, `ttlSecondsAfterFinished`) whose single pod
   runs the existing `booth-pipeline-runner` entrypoint with `max_concurrent=1`, the bearer from
   that Secret, and **exactly today's runner pod hardening** (non-root, fixed uid, read-only root
   filesystem, all capabilities dropped, seccomp RuntimeDefault, `automountServiceAccountToken:
   false`, a no-RBAC ServiceAccount). The Secret is then owner-referenced to the Job.
3. Waits for the pod to be Ready (its readiness probe is the service's `/healthz`), reads its pod IP.
4. Runs the task with `RemoteRunner(http://<podIP>:8080, <bearer>)`: the full invocation goes in the
   `POST` body (no 1 MiB limit; the service already accepts 64 MiB), logs and the result stream back
   exactly as today, and token refreshes are `PUT`s — **instant**, as today.
5. **Always deletes the Job** in a `finally` (success, failure, timeout or cancellation), which
   garbage-collects its pod and Secret. Cancellation is hang-up (the task is killed at once) plus
   Job deletion.

### How this meets ADR 0096

| ADR 0096 requirement | Here |
|---|---|
| One Job, one pod, one task | Yes — `max_concurrent=1`, one Job per attempt |
| Same harness, same image, same hardening, unloosened | Yes — the identical runner entrypoint and security contexts |
| Narrow namespace-scoped RBAC on the API/scheduler pod only | Yes — and **narrower**: `jobs` create/get/list/watch/delete, `pods` get/list/watch, `secrets` create/delete. No `pods/log` (logs arrive on the stream) |
| `automountServiceAccountToken: true` on the API/scheduler pod only | Yes; task pods stay `false` |
| Per-task Secret, owner-referenced to the Job | Yes — but it holds the per-task runner bearer, not the platform token (which travels and refreshes over the authenticated stream, as today) |
| Logs into the same log store | Yes — unchanged above the `Runner` seam |
| Cancellation by Job deletion | Yes (plus instant hang-up) |
| `maxConcurrent` caps outstanding Jobs | Yes |
| Retire the shared runner Deployment | Yes — no long-lived runner pod at all |

### Differences from ADR 0096's prescribed mechanisms (rulings requested)

- **Token delivery** over the authenticated per-pod stream (`PUT …/token`), not a Secret volume.
  Residual limit 2 (Secret-volume propagation latency) disappears: refresh stays instant. The
  platform token is never stored in a Kubernetes object at all, so nothing with `secrets` read
  access could ever read one.
- **Logs and the result** over that same stream, not `pods/log`. Avoids the sentinel and log
  rotation problems, keeps the 1 MiB result path as it is, and drops `pods/log` from the Role.
- **The runner `NetworkPolicy` is re-targeted to task pods**, not removed: same egress rules (DNS,
  core, opt-in booth-database, `extra`, opt-in internet), ingress only from the API/scheduler pod
  on 8080. Task pods therefore cannot reach each other at all.

### Isolation, against the vector that was found

Each task is its own pod: its own filesystem, `/tmp`, process and `/proc` namespace, so the
sibling-workdir and `/proc/<pid>/environ` routes have nothing to reach. No task pod has a
Kubernetes API credential, so it cannot read another task's Secret, pod or logs. NetworkPolicy
blocks task-to-task connections, and each pod accepts only its own per-task bearer. The required
test exercises all of it on a real CNI: two concurrent tasks from different workspaces, each
attempting the original attack (list `/tmp`, scan `/proc`), plus the Kubernetes API, plus a
direct connection to the other's pod — each must fail.

## A clarification needed: the non-Kubernetes path

ADR 0096 retires "`SubprocessRunner`'s multi-task-in-one-pod path … not a config flag". Proposed
reading: **the chart offers only Job-per-task** (the shared runner Deployment and `runner.enabled:
false` in-API-pod execution both go away). `SubprocessRunner` itself stays, as (a) what each Job
pod's runner service uses for its one task, and (b) the local-dev (`BOOTH_PIPELINE_DEV_MEMORY`, no
cluster) and unit-test path, which a chart install can never select.

## To measure, not assume (after building)

Cold-start latency per task on a real cluster (Job create → pod Ready → first log line), versus
today's subprocess spawn, including for a frequent short schedule. Token-refresh latency should be
unchanged (still an in-process write in the task's pod), and will be measured to confirm it.
