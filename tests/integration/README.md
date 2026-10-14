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
chart's Job template, on Calico (`cni/`, digest-pinned: kind's default kindnet does not reliably enforce egress) — to prove the isolation property (two concurrent
tasks from different workspaces cannot reach each other's token, Secret, pod or logs), that finished tasks leave nothing
behind, the ADR 0092 booth-database opt-in, and to measure task-pod cold start (written to the job summary). **`egress.sh`** proves the task pods' egress policy (ADR 0057) from inside real task pods: every
denial is reachable with the policy deleted, blocked while it is applied, and reachable once it is deleted again, on
the same address and port, in the default closed mode and with `allowInternet`, including a LAN host in each private
range (`cni/lan-targets.sh`). **`pip.sh`** proves a task's own Python packages (ADR 0115) against an in-cluster
index (`fixtures/pypi/`: the real requests/beautifulsoup4 wheels, hash-locked, plus a probe sdist whose build runs in
the pip init container): with no route a task fails at once; the installer reaches only the index (works-blocked-works
on every `egress.sh` target, from the init container and the task's code); it holds no token or credentials (a file
and process scan that must find a planted decoy); failed and unavailable installs fail clearly; and requests +
beautifulsoup4 install and import with `allowInternet=true`. It does
**not** prove core reconciles the module or that the shell mounts the UI: that is `booth-e2e`.
