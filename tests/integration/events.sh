#!/usr/bin/env bash
# Event triggers (ADR 0123; docs/design-event-triggers.md) on the real cluster: the deployed API pod's own
# JetStream consumer and scheduler, a real NATS with JetStream (fixtures/nats.yaml, installed with the
# chart), real task Jobs. The driver only writes triggers to the database and publishes to the bus; the
# deployed pod does everything else.
#
# What it proves (task_jobs_driver.py events, events-burst, events-after-restart):
#   - a burst of 74 files makes exactly one run, which is an event-fired run whose task sees ctx.trigger;
#   - events during a run make exactly one follow-up under queue_one and none under skip;
#   - a file written by the pipeline's own run doesn't fire it again;
#   - the pod restarted mid-debounce: the burst still fires, once.
# Not proved here: the bus credential (core's own tests) and real booth-storage and booth-api publishers
# (their own Integration). Duplicate deliveries, a lost consumer and an outage longer than the stream
# keeps events are in tests/unit/test_event_bus_nats.py, against a real JetStream server.
#
#   events.sh REPO-ROOT
set -euo pipefail
repo=${1:-.}
here="$repo/tests/integration"
ns=booth-pipeline
drive() { kubectl -n "$ns" exec -i deploy/booth-pipeline -- python - "$@" <"$here/task_jobs_driver.py"; }

echo "=== bursts, queue_one, skip, the loop guard"
drive events

echo "=== a burst, then the pod restarted before it fires"
drive events-burst
kubectl -n "$ns" rollout restart deploy/booth-pipeline
kubectl -n "$ns" rollout status deploy/booth-pipeline --timeout=180s
drive events-after-restart

echo
echo "events: OK"
