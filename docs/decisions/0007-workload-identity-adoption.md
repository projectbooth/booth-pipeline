# 0007: Adopting workload identity (ADR 0056/0057/0058/0059) — judgment calls

Status: **built; the gateway gap it originally routed is resolved by ADR 0059 (see §1). The judgment calls below are ratified
by the coordinator; §2 (nothing yet run against a real core) remains.** This closes
[0002](0002-execution-isolation.md) and [0005](0005-run-identity-and-data-access.md).

## What was built

- **`ctx.storage` / `ctx.catalog`** for tasks that opt in (stdlib-only client inside the task subprocess):
  `storage.backends/list/read/read_text/write/delete`, `catalog.datasets/dataset/register_dataset`. Locations are
  `{backendId, path}` (ADR 0045); the catalog registers the dataset a run produced.
- **Minting** (`workload.py`), in the API/scheduler pod only: `POST` to core with `{workspace, subject, roleCeiling, owner}`,
  authenticated with the credential from Secret `booth-workload-minting-credentials`.
- **The runner pod** (`runner_service.py`, `runners/remote.py`, `charts/.../runner*.yaml`): user code executes in a separate
  Deployment with no module credential, `automountServiceAccountToken: false`, a NetworkPolicy admitting only the API pod
  and offering no route to the database. The API pod drives it over an authenticated NDJSON stream; cancellation is hanging up.
- **403 handling:** a refused mint fails that task with *"not given platform access: the owner of this run hasn't signed in
  to the platform recently enough…"* in the task's error, the run's error and the run log — **never retried** (the owner has
  to act). Every other mint failure (network, 5xx) is an ordinary retryable task failure.

## 1. Gateway routing — RESOLVED by ADR 0059 (core `672032c`)

The first pass found that core's gateway (`/modules/{id}/*`) trusted only the OIDC provider, so a workload token sent
through it would 401, and shipped with a direct-Service workaround (`BOOTH_PIPELINE_STORAGE_URL` / `_CATALOG_URL` plus
`runner.networkPolicy.egress.extra`). ADR 0059 fixed it in core: the gateway now dispatches on `iss` and accepts a workload
token exactly like a human one. **The default is therefore the gateway: `BOOTH_CORE_URL/modules/{storage,catalog}`**, with
only `X-Workspace` sent (the gateway sets `X-Booth-Workspace` itself). The workaround is gone from the defaults: the runner's
NetworkPolicy allows DNS + core and nothing else, and `egress.extra` is empty.

The two base-URL settings remain as an **escape hatch** for unusual network topology. Pointing them at a module's own Service
means the runner's NetworkPolicy must allow it (`egress.extra`), and the client then also sends `X-Booth-Workspace` (which
a directly-called module reads). Note from ADR 0059: a workload token is 401 on every core route *except* the gateway, so a
token that works for a storage/catalog call will 401 if pointed at a core API route — we only ever call the gateway.

## 2. Still unverified against real services

Everything is tested against **fakes of core, storage and catalog** (the gateway fake mimics core's `/modules/{id}/*` prefix
stripping and accepts only `X-Workspace`) that follow the ADRs and the sibling code as read
(request/response shapes, the `subject` grammar `^[a-z][a-z0-9-]{0,31}:[A-Za-z0-9._:-]{1,200}$`, the Secret keys). It has
not run against a real `booth-core` minting, nor a real storage/catalog trusting core's issuer. `booth-e2e` is the check.

## Judgment calls (all cheap to change)

| Call | Why |
|---|---|
| **Per-task opt-in** (`platformAccess`, default off) rather than every task of a job getting a token | A pipeline that never touches storage must not be blocked by a lapsed owner or hold an identity it doesn't use. A task without it gets a clear "no platform access" error naming the fix. |
| **`subject` = `run:<run-id>`** (not `job:<id>`) | Core's audit logs then join directly to our run history; still `<kind>:<id>`, still never a person. |
| **Owner:** scheduled run = the job's creator; **manual run = the person who pressed Run now** | A manual run has a present, freshly-authenticated human (no recency 403 surprise); an unattended one needs a stable owner. Editing a job **never moves ownership** (a moved owner would launder identity); a job with no owner (created before this) is adopted by whoever saves it next, and its runs say so until then. |
| **`roleCeiling` is a job setting: `editor` (default) or `viewer` — never `owner`** | Nothing a pipeline does needs workspace administration; a token that cannot administer cannot be turned into one. Core additionally caps it at the owner's live role. |
| **Token refresh is *pushed*** by the API pod every 4 min to the runner hosting the task, written atomically to a 0600 file the task client re-reads per request | A token lives ~10 min, tasks run hours, and only the API pod may mint (ADR 0057). |
| → **runner `replicaCount` must be 1** (chart refuses more) | The push must reach the pod running the task; a load-balanced Service can't promise that. Capacity comes from `maxConcurrent` + pod resources. Removing this needs either sticky routing or the runner pulling refreshes — not v0. |
| **Runner auth = one shared bearer secret**, chart-generated and kept across upgrades | Simple and sufficient with the NetworkPolicy; not mutual TLS. The task token travels in the POST body over cluster HTTP — acceptable inside a NetworkPolicy'd namespace, worth TLS/mesh later. |
| **Runner scrubs its own environment from tasks** (tested: the task sees only `BOOTH_RUN_ID`, `BOOTH_TASK_KEY`) | The runner's own bearer must not be readable by task code either. |
| **Refuse to start** if a minting credential is mounted while tasks would run in-pod; chart refuses `runner.enabled=false` with workload identity | The one mistake that would put the credential in task code's reach must be impossible, not merely documented. |
| The chart's NetworkPolicy egress is an allowlist: DNS + core; internet opt-in and never private ranges | "No route to the database" by construction. **Requires a CNI that enforces NetworkPolicy** (silently inert otherwise). |

## Still not done

- A task running past its owner's recency window: the *refresh* fails mid-run; the task gets a log line
  ("could not refresh… its access ends when the current token expires") and its calls 401 when the token lapses.
- Runner pods are not yet limited per task (no cgroup per subprocess); a tenant can still exhaust the runner's resources.
- Serial execution ([0003](0003-dagster-usage.md)) is unchanged; the runner *could* now host parallel tasks.
- No UI to see which runs used platform access beyond the run log/error; core's mint log (module, subject, workspace,
  owner, ceiling, granted role) is the audit trail.
