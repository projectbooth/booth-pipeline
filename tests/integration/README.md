# Integration tests

Two different things live here — don't confuse them with testing-strategy layer 3.

**`test_real_stack.py`, `test_image.py`** — run by a developer (or CI's `image` job for the latter): the real app against
real PostgreSQL + real Keycloak, and the built image executing tasks under the chart's security context. They need
Docker/Keycloak and skip themselves when it is absent. See the repo README for the commands.

**`../../.github/workflows/integration.yml` + `fixtures/`** — layer 3 proper: deploy the chart to a `kind` cluster.
It uses a *vendored copy* of booth-core's `BoothModule` CRD and a stand-in PostgreSQL + credentials Secret, not a real
pinned `booth-core`, so it proves the chart deploys, registers the right fields (checked on the live resource, since a
CRD that doesn't know a field prunes it), serves `/healthz`, refuses unauthenticated calls, and has no Kubernetes API
access. It does **not** prove core reconciles the module or that the shell mounts the UI: that is `booth-e2e`.
**It has never been executed.**
