# Integration tests

Two different things live here — don't confuse them with testing-strategy layer 3.

**`test_real_stack.py`, `test_image.py`** — run by a developer (or CI's `image` job for the latter): the real app against
real PostgreSQL + real Keycloak, and the built image executing tasks under the chart's security context. They need
Docker/Keycloak and skip themselves when it is absent. See the repo README for the commands.

**`../../.github/workflows/integration.yml` + `fixtures/`** — layer 3 proper: deploy the chart to a `kind` cluster.
It uses a *vendored copy* of booth-core's `BoothModule` CRD and a stand-in PostgreSQL + credentials Secret, not a real
pinned `booth-core`, so it proves the chart deploys, registers the right fields (checked on the live resource, since a
CRD that doesn't know a field prunes it), serves `/healthz` and refuses unauthenticated calls. It then runs real
task Jobs (ADR 0096) from inside the API pod with **`task_jobs_driver.py`** — the API pod's own token, Role and the
chart's Job template, on a CNI that enforces the task NetworkPolicy — to prove the isolation property (two concurrent
tasks from different workspaces cannot reach each other's token, Secret, pod or logs), that finished tasks leave nothing
behind, the ADR 0092 booth-database opt-in, and to measure task-pod cold start (written to the job summary). It does
**not** prove core reconciles the module or that the shell mounts the UI: that is `booth-e2e`.
