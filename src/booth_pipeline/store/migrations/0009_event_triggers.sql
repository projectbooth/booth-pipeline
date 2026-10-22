-- ADR 0123: event triggers. A pipeline's event trigger sits beside its schedule (one time trigger and one
-- event trigger), and a run records what fired it.
ALTER TABLE pipelines ADD COLUMN event_trigger jsonb;
ALTER TABLE pipelines ADD COLUMN event_trigger_since timestamptz;
ALTER TABLE runs ADD COLUMN trigger_events jsonb;

-- Where each pipeline's event trigger stands, so a restart neither loses a pending fire nor runs it
-- twice. The scheduler claims due rows FOR UPDATE SKIP LOCKED, as it claims schedules.
CREATE TABLE event_trigger_state (
    pipeline_id           text PRIMARY KEY REFERENCES pipelines(id) ON DELETE CASCADE,
    workspace             text NOT NULL,
    pending_since         timestamptz,
    last_event_at         timestamptz,
    fire_at               timestamptz,
    queued                boolean NOT NULL DEFAULT false,
    last_fired_at         timestamptz,
    events                jsonb NOT NULL DEFAULT '[]',
    event_count           integer NOT NULL DEFAULT 0,
    missed_events         boolean NOT NULL DEFAULT false,
    ignored_own_runs      integer NOT NULL DEFAULT 0,
    skipped_while_running integer NOT NULL DEFAULT 0,
    last_event            jsonb,
    gap_detected_at       timestamptz
);
CREATE INDEX event_trigger_state_fire_at ON event_trigger_state (fire_at) WHERE fire_at IS NOT NULL;
CREATE INDEX event_trigger_state_queued ON event_trigger_state (pipeline_id) WHERE queued;

-- JetStream stream sequences already handled: a redelivered message is acked and does nothing.
-- Pruned once older than the stream keeps messages.
CREATE TABLE trigger_events_seen (
    stream_seq bigint PRIMARY KEY,
    seen_at    timestamptz NOT NULL
);
CREATE INDEX trigger_events_seen_at ON trigger_events_seen (seen_at);

-- The shared consumer's own record: it exists once the consumer has been created, and last_contact_at
-- is the last time any replica was connected and reading. A longer silence than the stream keeps
-- events means events may have been lost.
CREATE TABLE trigger_bus (
    name            text PRIMARY KEY,
    created_at      timestamptz NOT NULL,
    last_contact_at timestamptz NOT NULL
);
