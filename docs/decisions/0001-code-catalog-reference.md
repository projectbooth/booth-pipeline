# 0001: How a Task references cataloged code (the ADR 0010 "narrow shape")

Status: **proposed by booth-pipeline; catalog's side recorded as compatible. Needs the coordinator to ratify or
close** — it is pipeline-specific by design (ADR 0010) and must not be generalised into
`contracts/runnable-code.md` unless a second consumer needs it (`ARCHITECTURE.md` §7 item 6).

## The shape

```json
{ "type": "catalog", "entryId": "<catalog code entry id>", "version": "<label | latest>" }
```

That is the whole reference. At **save time** the pipeline backend resolves it, with the *caller's own token* through
booth-core's gateway (`GET /modules/catalog/api/code/{id}/versions/{version}` and `/code/{id}`), and stores a
**snapshot** beside it: the concrete `version` label (never `latest`), the entry `name`, `language`, the `source`
and a `sha256` over its UTF-8 bytes. **A run never calls the catalog.**

Why snapshot rather than resolve at run time:

- A scheduled 3 a.m. run must not depend on the catalog being up, installed, or permitting a service identity we don't
  have (see 0005). With snapshots the base runner works with the catalog entirely absent (tested).
- `latest` is pinned when the user saves, not silently re-resolved by the scheduler — a saved pipeline version is
  immutable and reproducible.

## What we rely on (booth-catalog `docs/decisions/0006`, "Guarantees a consumer may rely on")

Verified by reading catalog's code at `166d6ab` (its tests were not run from here):

| Guarantee | Where it matters here |
|---|---|
| A published version's source is immutable; republishing a label is 409 | the snapshot can never disagree with "that version" |
| `versions/latest` returns the concrete `version` label (highest `seq`, not highest label) | we store the concrete label |
| A deleted or other-workspace entry is 404 on every read | mapped to a 422 on the task's `code` field |
| Catalog's 404 is JSON `{"error"}`; the gateway's "module not found" 404 is plain text | the client distinguishes "no such entry" from "catalog not installed" by body |

Explicitly **not** assumed (catalog says it does not promise them): entry deletion cascades all versions; the 1 MiB
source cap is operator-configurable; `language` is a free advisory label; entry ids are stable but *names* are reusable
(we key on id).

## Pipeline-side rules

- A client-supplied `source`/`sha256`/`name` is **never trusted** — it would let a caller label arbitrary code
  "catalog entry X @ 1.0". A catalog reference is re-fetched at save unless it matches a snapshot in *our own*
  previous version of that pipeline (which also lets an unrelated edit be saved while the catalog is down).
- The base runner runs Python. A catalog entry whose `language` is not `""`/`python` is refused for a base-runner
  task at save time, with the entry named.
- Task code contract (ours, not catalog's): define `run(ctx)`; `ctx.inputs`, `ctx.params`, `ctx.log`; the return value
  (JSON, ≤ 1 MiB) is the task's output. A script with no `run` is executed top to bottom. Catalog defines no
  entrypoint or dependency metadata and we did not ask it to.

## "The user's own workspace" — an interpretation to confirm

The brief says code comes "from `booth-catalog`'s code catalog, or the user's own workspace". No storage-backed source
exists to build against, so we read "own workspace" as **code written in the builder** (`{"type":"inline","source"}`),
stored inside the pipeline version. It is also what makes the base runner work with zero other modules. If it meant
"a file the user keeps in `booth-storage`", that is a second reference type (`{backendId, path}`, ADR 0045) and would
also need the run identity question in 0005 answered.

## Coordination status (honest)

booth-catalog's commit `166d6ab` says booth-pipeline "proposed resolving and snapshotting … at save time and asked for
confirmation". **This session has no record of that exchange** (the repo was empty and no peer agent was reachable),
so the confirmation was taken at face value only after checking it against catalog's source. It asks nothing new of
catalog. If a second consumer (`booth-notebooks`, `booth-streamlit`) needs "run cataloged code", this becomes the
input to the ADR proposing the shared contract — not something either module should copy quietly.
