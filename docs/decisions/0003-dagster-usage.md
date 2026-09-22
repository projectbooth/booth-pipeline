# 0003: How Dagster is used (and where v0 departs from ADR 0010's wording)

Status: **accepted by booth-pipeline as implementation judgment; flagged because two points read differently from
ADR 0010's text.** No coordinator action needed unless you disagree.

ADR 0010 fixes Dagster as the engine and a custom builder as the authoring UI. It does not say *how* Dagster is
embedded. Choices made, and why:

| Choice | Why | Departure from ADR 0010 wording? |
|---|---|---|
| **Dagster as a library** — a saved spec is compiled to a `JobDefinition` per run and executed in-process | No code locations, webserver or daemon to deploy; pipelines are database rows, not Python files | no |
| **Op/graph jobs, not asset jobs** | ADR 0010 says "job/op definitions"; the asset-model fit with the catalog it also mentions needs pipelines to register outputs as catalog assets, which is unspecified (and `ARCHITECTURE.md` §7 item 12 forbids extending the catalog's asset model unilaterally) | **yes**, mildly — "asset-based model" is the *rationale*, not what v0 builds |
| **Our own run store, not Dagster's** (`DagsterInstance.ephemeral()` per run) | Run history, per-task state and logs need workspace scoping, retention and an API we control; Dagster's instance is neither | no |
| **Our own cron scheduler** (a polling loop with atomic claiming, coalescing missed fires) rather than Dagster's daemon | Dagster's scheduler watches code locations; ours would mean generating and hot-reloading a code location on every save | **yes** — "schedule" is a Job feature we implement, not Dagster's |
| **Serial execution** (in-process executor) | Correct and simple; parallel branches need the multiprocess executor plus a persistent instance | limitation, not departure |
| **Runners are Dagster-agnostic** (`Runner.run(invocation)`); Dagster decides *when*, the runner decides *where* | This is how "base runner by default, Spark only if selected" (ADR 0006) stays a per-Task choice | no |

## Engine facts we hit (each has a regression test)

- A hand-built `OpDefinition` whose compute function is not a **generator** has its `RetryPolicy` silently ignored — the
  task fails on attempt 1 and nothing says why. Ours `yield Output(...)`.
- Dagster rejects an annotated `context` parameter that is not one of its own types.
- An ephemeral instance must be disposed on the thread that created it or every run logs SQLite thread errors.
- Op/input names are prefixed so a user task key like `context` or `config` cannot collide with reserved names.

## Consequences

- Parallel independent branches are not parallel in v0 (documented in the README). Revisit with the runner
  isolation decision in 0002, since a remote runner naturally allows concurrency.
- If the coordinator wants Dagster's own UI/asset lineage later, the compile step (`engine.compile_job`) is the one
  place that would change; the spec, store and API would not.
