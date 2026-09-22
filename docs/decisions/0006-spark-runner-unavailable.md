# 0006: Spark is listed but unavailable — booth-spark defines nothing to target yet

Status: **accepted; nothing to decide now, recorded so nobody wonders.**

The brief: a Task targets the base runner by default, and Spark "only if installed and explicitly selected". ADR 0006
gives `booth-spark` the job of defining "a documented compute-submission interface for its own sake". That interface
does not exist yet (`booth-spark` is `not started`).

We therefore built the **seam** — `Runner` (`runners/base.py`), a `RunnerRegistry`, per-Task `runner` in the spec, and
save-time validation that a task's runner is registered and available — but registered **only `base`**.
`GET /api/runners` lists Spark as `available: false` with the reason, the builder shows it disabled with that reason,
and a save naming `spark` is a 422. Inventing a submission protocol here would be designing booth-spark's contract for
it, and would make Spark a de-facto dependency (ADR 0006 forbids that).

When `booth-spark` publishes its interface, a `SparkRunner` is one class plus one registry line, gated on both "the
module is installed" (core's module registry) and "the user selected it". No spec or API change.
