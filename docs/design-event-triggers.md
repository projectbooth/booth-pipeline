# Design: event triggers for pipelines (ADR 0123), booth-pipeline's part

Status: **accepted and built** (ADR 0123, accepted 2026-10-21 with the rulings in its "Revision 2026-10-21"
section, which took this note's sections 1-7 and answered section 10: `ctx.trigger.events` yes, first 100 plus a
count; one time trigger plus one event trigger; fire once when events may have been lost; the loop guard keyed on
the run). Section 11 lists where the build differs from the proposal below. Inputs: booth-architecture ADR 0123,
0007, 0021, 0026, 0046, 0049-0051, 0065, 0117, 0120; this repo's `docs/decisions/0009` (how the interval trigger was
added), `scheduler.py`, `service.start_scheduled`, `runs.py`; booth-core `internal/natsauth/permissions.go` and
`internal/eventbus/nats.go`; booth-catalog `internal/events/nats.go` (the one working JetStream consumer).

## 1. Model

**`EventTrigger` is a new member of the `Trigger` union** (`type: "event"`), validated like the other two. But
it sits in its own field, `Pipeline.event_trigger` (a new jsonb column; a new migration after PR #21's `0008`), **not** in
`schedule`. A pipeline then has at most one time trigger (cron or interval) and at most one event trigger. ADR
0123 point 6's safety net needs both on one pipeline, and today a pipeline has exactly one `schedule`. A general
list of triggers would change `next_run_at`, the claim query, the API and the editor for no current need.
Existing rows are untouched: `event_trigger` is NULL, and `schedule` keeps its shape and `_coerce_trigger`. The
wire gains `eventTrigger` on `PUT /pipelines/{id}/schedule` and on the pipeline body.

```
{type: "event", enabled: true,
 match: [{storage: {backendId: "local", prefix: "finance/dev/inbox/"}},
         {api: {slugs: ["finance-dev-tx"]}} | {api: {slugPrefix: "finance-dev-"}}],   # 1..10 entries
 debounceSeconds: 10 (1..3600), maxWaitSeconds: 60 (debounce..86400),
 whenRunning: "queue_one" | "skip", minGapSeconds: 5 (MIN_INTERVAL_SECONDS..86400)}
```

**Save-time checks** (pure, plus one call):
- A prefix is cleaned as storage cleans paths: no `..`, no leading `/`, and a trailing `/` added.
- An empty prefix (the whole backend) is allowed, with a UI warning.
- `backendId` must exist; checked as the caller through the storage client pipeline already has.
- Slugs and prefixes match booth-api's slug grammar. An unknown slug is a warning, not a refusal: a prefix is meant
  to match APIs made later.
- If the bus isn't configured (no `booth-event-bus-credentials` Secret), the save is refused with a reason, the
  way an unavailable Spark runner is.

## 2. The consumer

**One shared durable pull consumer, `booth-pipeline-triggers`**, not one per pipeline. Pipelines are database
rows that come and go; consumers are JetStream objects that pipeline may create but, by core's grants, may not
delete. Every replica pulls from it and the work is shared, as in booth-catalog.
- **Filter:** `booth.*.storage.object.created` and `booth.*.api.write.committed`. With NATS 2.10+,
  `FilterSubjects` takes both; otherwise two consumers. Core is asked which.
- **Ack:** explicit, `AckWait` 30 s, `MaxAckPending` 1000.
- **First creation is `DeliverNew`.** Enabling this must not replay the stream's 7 days of history into new triggers.

Handling one message, in one database transaction, then ack:
1. Parse the envelope. The subject's workspace and type must equal the envelope's (ADR 0046 rule 4); otherwise
   the event is dropped and counted.
2. Find enabled event triggers in that workspace whose `match` covers the event: same backend with the path
   under the prefix, or the slug in the list or under the prefix. An event published before the trigger was
   enabled is ignored.
3. Apply the loop guard (section 4), then update each matching trigger's state (section 3).
4. Record the JetStream stream sequence in `trigger_events_seen` (pruned after the stream's 7-day `MaxAge`). A
   sequence already there is a redelivery: ack and do nothing. **A duplicate delivery therefore never adds a
   run**, even one that arrives after the burst fired.

**Restart:** messages not yet acked are redelivered, and the sequence table absorbs any that were committed but
not acked. **Long outage:** the backlog is replayed on return. Debounce runs on processing time, so a replayed
burst of any size becomes one run per trigger. **Outage longer than 7 days** (or a consumer recreated): if the
consumer's next sequence is below the stream's first sequence, events were lost. The status line then says so,
and I recommend firing each enabled event trigger once (Q3). The slow time trigger remains the real safety net.

## 3. Debounce and queue-one state: in PostgreSQL, fired by the existing scheduler

Table `event_trigger_state`, one row per pipeline:
- `pending_since`, `last_event_at`, `fire_at`;
- `queued` (queue-one memory), `last_fired_at`;
- `events_since_fire`, `ignored_by_loop_guard`;
- `last_event` (`{at, kind, backendId, path | apiSlug, table, operation, principal}`);
- `gap_detected_at`.

A matching event sets `pending_since` if empty, then:
`fire_at = max(min(now + debounce, pending_since + maxWait), last_fired_at + minGap)`.

**Firing reuses the scheduler unchanged in shape.** The index and the claim query also read `fire_at`. The claim
takes the row `FOR UPDATE SKIP LOCKED`, exactly as `claim_due_pipelines` does now. Then:
- **No active run** (or `allowConcurrentRuns`): start the run with trigger `event`, acting as the owner.
- **A run is active and `queue_one`:** set `queued`, clear the pending fields; nothing starts.
- **`skip`:** clear the pending fields; nothing starts.

When a run of the pipeline ends, `RunManager` checks `queued` and, if set, sets `fire_at = now + minGap`. The
sweep does the same, for a worker lost mid-run.

`skip` also drops events that arrive while a run is active (match step 3 sees the active run). `queue_one` keeps
them, and they fold into the one follow-up run. Nothing is held in memory, so a restart can neither lose a
pending fire nor run it twice: the claim is the single place a run starts.

## 4. Loop guard: key on the run's subject, not on the owner (changes ADR 0123 point 3)

ADR 0123 says to ignore events whose principal is "the workload identity the pipeline runs as". That is the
owner, a person's `sub` or `svc:<ws>:<name>`. The trouble is the owner's own uploads: a person who owns
the pipeline and drops a file through the Storage page has principal = their `sub`. Keyed on the owner, that
event would be ignored, which is the opposite of what's wanted.

What a run actually writes with is its own subject, `pipeline:<ws>:<runId>` (ADR 0120, built in PR #16). booth-
storage and booth-api see that as the token's `sub`. So the guard is: **if `principal` is
`pipeline:<ws>:<runId>` and that run belongs to this pipeline, ignore the event** (one indexed lookup) and count it.
This needs `principal` to be the verified token `sub`, verbatim. Writes that bypass both modules (Spark, direct
S3 or Postgres) carry no event, so they cannot loop (ADR 0123 point 5).

## 5. Authorization (changes ADR 0123 point 4's mechanism, not its intent)

I checked what reading means today:
- **booth-storage:** every route under `/api/backends` needs only `CanRead`, which is any workspace member. There
  is no per-prefix read rule.
- **booth-api:** `GET /api/apis` lists the workspace's APIs to any member.

So "the Runs-as identity can read the watched path or APIs" is the same as "the Runs-as identity is a workspace
member". That is already enforced on every event-fired run by the scheduled-run authorization at start (core
mints for the owner, a disabled service account is refused; ADR 0120). Any task's data access is the owner's,
capped as today.

So save-time checks need only: the caller is an editor or owner (`PUT /schedule` already requires it), the
backend exists, and the owner rules stay as they are. **No new route is needed in booth-storage or booth-api.**
If storage ever adds per-prefix reads, the check becomes a probe `GET …/objects?prefix=` with a token minted for
the owner. I ask storage to say if that is planned.

Events reveal paths and API names. The "last event" detail (section 6) is therefore shown to editors and owners;
viewers see only the time.

## 6. UI (Schedule & Triggers tab)

- An **"Event trigger"** card under the existing schedule card. It has:
  - match rows (Storage: backend picker and folder; API: slugs or "slugs starting with");
  - debounce and max-wait fields, "If a run is active: run once more afterwards / ignore", minimum gap;
  - the same pause checkbox.
- **Status line:**
  - `Last event 14:02:11 — finance/dev/inbox/2026-10-21-ab12cd-stmt.csv (streamlit:home:finance-dev)`;
  - `Next run: in 7 s (3 events; waits up to 60 s)`, or `Waiting for the active run, then runs once`;
  - `Ignored 2 events written by this pipeline's own runs`;
  - a warning when a gap was detected.
- **Help text:** files written straight to object storage (Spark, the lakehouse) and rows written straight to
  PostgreSQL by a task don't trigger. Keep a slow schedule as a safety net. A pipeline that writes into a folder
  it watches through another identity can trigger itself.
- The run list shows trigger "Event" with a one-line summary, e.g. `3 files in finance/dev/inbox/`.

## 7. Concurrency and several triggers

- **`allowConcurrentRuns: true`:** `whenRunning` doesn't apply (the editor hides it). Each burst starts its own
  run, spaced by `minGap`.
- **`allowConcurrentRuns: false`:** `whenRunning` applies, whatever started the active run (Run now, cron,
  interval or event).
- **The time trigger is unchanged.** A cron or interval fire during an active run is still skipped. The event
  trigger's `queued` follow-up is the only queueing.
- **One run can serve both triggers.** If the time trigger fires while an event burst is pending, the event fire
  that follows finds that run active and queues behind it.

## 8. Tests

Unit tests (memory and PostgreSQL stores, a fake bus feeding the handler directly):
- model bounds and back-compatibility (a NULL `event_trigger` row loads);
- matching: prefix boundary (`inbox/` doesn't match `inbox2/`), slug prefix, wrong workspace, subject/envelope
  mismatch;
- **74 events in a burst → 1 run.** Also: max-wait fires during a steady stream; min-gap holds.
- **Event during a run:** under `queue_one`, exactly one follow-up run (from 1 event or 30); under `skip`, none.
- **Duplicate delivery** (same stream sequence, before and after the fire) → no extra run.
- **Loop guard:** this pipeline's own run is ignored; another pipeline's run and the owner's own upload are not.
- **Restart:** a crash between commit and ack loses nothing and doesn't double-fire. Two replicas claiming at
  once → one run.
- **Gap detection.**

Integration (kind): real NATS with JetStream and the stand-in core minting a bus credential. The stand-in
publishes `storage.object.created` the way storage will. The test runs a 74-event burst to one run, a
follow-up under `queue_one`, and a restart of the pipeline pod mid-debounce.

## 9. What I need from other modules

- **booth-core:**
  - grant `events.subscribe: ["storage.object.created", "api.write.committed"]` from pipeline's manifest (no
    code, if the existing mechanism covers three-token types, which the regex suggests);
  - confirm the NATS server is 2.10+ (for multiple filter subjects);
  - confirm only booth-storage may publish `storage.*` and only booth-api `api.*` (ADR 0051), since the loop guard
    and the "last event" line trust `principal`;
  - keep `BOOTH_EVENTS` at ≥ 7 days `MaxAge`.
  - Core's known limit that consumer APIs aren't name-scoped stays as documented.
- **booth-storage and booth-api:**
  - `principal` = the verified token `sub`, verbatim;
  - storage's `path` relative to the backend root with no leading `/`, cleaned the same way as on write;
  - publish after commit, with a deterministic `Nats-Msg-Id` (for example the object's backend, path and
    version, or booth-api's request id or idempotency key), so JetStream's duplicate window drops publisher
    retries;
  - booth-storage: say whether per-prefix read ACLs are planned (section 5).

## 10. Open questions for the user

1. **Should a run see what triggered it?** For example `ctx.trigger.events`, at most 500 entries of
   `{backendId, path}` or `{apiSlug, table, operation, rows}`. The finance inbox could then load exactly the new
   files instead of listing the folder. I recommend yes: small, and it doesn't change the loop guard. Without it,
   tasks list the folder as they do today.
2. **One time trigger plus one event trigger per pipeline** (recommended), or a general list of triggers?
3. **When events may have been lost** (outage longer than the stream's 7 days), fire each event trigger once
   (recommended), or only show the warning?
4. **The loop guard keyed on the run's subject** (section 4) rather than the owner's identity: please confirm,
   since it changes ADR 0123's wording.

## 11. As built

Where the build differs from sections 1-9, or settles what they left open:

- **Not a member of `Trigger`.** `EventTrigger` has `type: "event"` but is its own model in its own field
  (`pipelines.event_trigger`, migration `0009_event_triggers.sql`; PR #21's task outputs use `0008`, and the two merge
  in either order). In the union, `schedule` could have held one.
- **"Before the trigger" is `event_trigger_since`.** Set when the trigger is turned on or its match changes. An event
  whose `publishedAt` is earlier is ignored (an unreadable time is let through). A publisher clock running behind
  pipeline's could drop an event published in the first seconds after a save; nothing else depends on clocks.
- **Lost events are detected by silence, not sequence numbers.** Every replica that is reading records the time
  (`trigger_bus.last_contact_at`, every 30 s). On (re)connect, a silence longer than the stream's `max_age` means
  events may be gone, and every enabled event trigger fires once with `missedEvents: true`. The stream is shared
  with other event types, so comparing sequence numbers would also count other modules' expired events. A consumer
  that has gone while this module had one is recreated from 60 s before the last contact (a replay; recorded
  sequences make the overlap harmless). First creation delivers new events only.
- **Firing.** The scheduler claims due rows `FOR UPDATE SKIP LOCKED`, as it claims schedules, and the consumer
  wakes the local scheduler after recording an event. A run that ends releases a queued follow-up in the same
  transaction as its status (`finish_run`). A claim that finds the pipeline busy because a run slipped in between
  is put back (`requeue_event_trigger`).
- **Save-time check.** Only Storage backends new to the save are asked about (`GET /backends/{id}` as the caller),
  so an unrelated edit saves while booth-storage is down. API slugs are not checked: a prefix is meant to match
  APIs created later, and booth-api's list needs a person's token anyway.
- **Who sees what.** An event-fired run's "triggered by" says "3 files" or "1 write" with no path. Its
  `triggerEvents.events` and the status endpoint's `lastEvent` detail are for editors and owners only. Viewers
  get the counts and the time.
- **The bus credential is the API pod's alone, and optional** (ADR 0123 revision 2026-10-22). It's mounted only
  there, and `config.py` refuses to start if it's present while tasks would run in the same pod. The mount is
  `optional`: nothing but event triggers needs the bus. Without the credential:
  - the pod starts;
  - the consumer idles and looks again every 15 s (a Secret written later is picked up without a restart);
  - the status endpoint and the Schedule tab say "Event bus unavailable", with the reason;
  - cron and interval schedules, Run now and every run are unaffected.

  Every other failure (a credential the bus rejects, the client unable to use it, no stream, the bus gone
  mid-session) only ends the consumer's own session, which it retries on its own thread.
  `tests/unit/test_event_bus_failures.py` proves each of these against real NATS servers, with an interval schedule
  firing throughout. Chart value `eventTriggers.enabled` (default on) declares `events.subscribe` and mounts it.
- **An older UI keeps the trigger.** `PUT /schedule` without `eventTrigger` keeps the saved one; `null` removes it.
- **Tests.**
  - `tests/unit/test_event_triggers.py`, against both stores:
    - a burst of 74 makes 1 run; the first 100 events plus the exact count;
    - max wait and minimum gap;
    - an event during a run makes 1 follow-up under queue_one (from 30 events) and none under skip;
    - duplicates before and after the fire;
    - the loop guard, including the owner's own upload firing;
    - workspaces, turning the trigger off, requeue, an outage backlog of 500 making 1 run, lost events;
    - 4 threads claiming at once, and a restart (a second PostgreSQL store) mid-burst.
  - `test_event_trigger_api.py`: save and validation, `ctx.trigger` end to end, viewers, the status line, and
    start-time authorization.
  - `test_event_bus_nats.py`, against a real JetStream server (now in `hack/docker-compose.test.yml`):
    - new-only on first creation;
    - a restart, a lost consumer, a long outage;
    - two replicas;
    - a redelivery after a failed write;
    - the app's own consumer and scheduler end to end.
  - `tests/integration/events.sh` (kind, the deployed pod's consumer, an unauthenticated stand-in NATS): in
    Integration.
  - `tests/integration/realstack-events.sh`, against the real publishers. Keycloak, booth-core (its authenticated
    NATS and `BOOTH_EVENTS`), booth-database, booth-catalog, booth-api and booth-storage are each built from source on
    kind, and every bus credential is core's. Through core's gateway only, it checks:
    - an upload fires a trigger;
    - a burst makes one run, and a trickle fires at the max wait;
    - queue_one makes one follow-up and skip none;
    - the run's own write through booth-storage is ignored;
    - an app's write through booth-api fires a trigger.
- **Found on the real stack: `nats-py[nkeys]`.** Core's bus credential is a `.creds` file, and nats-py signs in with it
  through `nkeys`, an optional extra. Without it the deployed consumer retried forever. A unit test now signs in with a
  credential file. Also confirmed there:
  - the publishers' `publishedAt` carries nanoseconds and parses;
  - `BOOTH_EVENTS` keeps a 2 h duplicate window and 7 days of events;
  - the consumer's two filter subjects work on core's NATS 2.14.6.
- **Workspaces on the bus.** Core grants `booth.*.<type>` (every workspace), not the per-workspace subjects ADR 0123
  point 4 names. The consumer reads every workspace's events and matches only triggers in the event's own workspace.
  The subject is authoritative, and an envelope that disagrees is dropped.

