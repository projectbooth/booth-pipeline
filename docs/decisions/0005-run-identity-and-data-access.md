# 0005: Runs have no platform identity — so tasks cannot yet read or write booth-storage

Status: **RESOLVED by ADR 0056/0058 and built — see [0007](0007-workload-identity-adoption.md)** (tasks that opt in get
`ctx.storage` / `ctx.catalog`, acting as a short-lived, role-ceilinged run identity). One gap remains routed there: core's
gateway does not yet verify workload tokens. Kept below as the record of the problem.
(Original status: OPEN — it limited what "source → transform → sink" could mean in v0.)

## What v0 can and cannot do

A **manual** run could in principle carry the triggering user's token; a **scheduled** run has no user at all. The
platform has no concept of a service/workload identity: identity is "a user's OIDC token through the gateway"
(ADR 0004/0007), and credentials for modules are Secrets provisioned ahead of time (ADR 0020), not live grants.

So a task's code has **no credentials for any other module** — deliberately (see 0002: anything handed to task code
must be assumed readable by every editor in the workspace). Consequences:

- Data passes *between tasks* as JSON return values (≤ 1 MiB each). That is enough for control-plane-sized data and for
  demonstrating the DAG, and not for real datasets.
- A source cannot read from, and a sink cannot write to, `booth-storage`; nothing can register output in
  `booth-catalog`. Tasks can still reach the outside world (HTTP, databases) with credentials in their own params —
  which are stored in the pipeline spec in the clear. **Do not put secrets in task parameters.**
- The brief's storage/catalog dependencies are therefore used only for what the *builder* needs (browse and snapshot
  cataloged code, at save time, as the saving user). No run-time storage integration exists.

## Questions for the coordinator

1. What identity does a scheduled run act as? Options: a per-Job service account minted by core (new core capability,
   ADR-level); the Job creator's delegated refresh token (long-lived user credential in a database — poor); or "runs get
   no platform identity" (today).
2. Given (1), how does task code reach `booth-storage`? A `ctx.storage` client that dials the gateway with the run's
   identity, using ADR 0045's `{backendId, path}` references, is the obvious shape, but only after (1) and 0002.
3. Should pipelines publish run/output events (`pipeline.run.finished`, …) for `booth-catalog` lineage? That needs the
   manifest `events` field (ADR 0050) and the catalog asset-model question (§7 item 12). Not built; the manifest
   correctly declares no events.

Nothing here should be solved inside this repo: a pipeline-specific answer would become the fleet's accidental one.
