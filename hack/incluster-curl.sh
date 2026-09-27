#!/usr/bin/env bash
# Runs curl once from a throwaway pod inside the cluster and prints curl's output — used by
# .github/workflows/integration.yml for its in-cluster checks. Usage:
#   hack/incluster-curl.sh <namespace> <curl args...>
#
# Deliberately NOT `kubectl run -i --rm`: that attaches to the pod only after it has started, and
# a curl against a ready in-cluster service can finish first, so its output is silently lost.
# That is exactly what kept the "/healthz is 200" check failing (the app logged the curl pod's
# request as `200 OK` while the step saw only kubectl's own messages). Here the pod runs to
# completion unattached and its output is read back from its logs, so nothing can be missed.
#
# INCLUSTER_LABELS (optional, "k=v,k=v") labels the pod — e.g. as this release's API component, the
# only pod the runner's NetworkPolicy lets through. Pass curl a --max-time so no check can hang.
set -euo pipefail

ns="$1"
shift
name="curl-$(date +%s)-$RANDOM"

labels=()
if [ -n "${INCLUSTER_LABELS:-}" ]; then labels=(--labels "$INCLUSTER_LABELS"); fi
kubectl -n "$ns" run "$name" "${labels[@]}" --restart=Never --image=curlimages/curl -- "$@" >/dev/null
phase=""
for _ in $(seq 1 120); do
  phase="$(kubectl -n "$ns" get pod "$name" -o jsonpath='{.status.phase}')"
  case "$phase" in Succeeded | Failed) break ;; esac
  sleep 1
done
kubectl -n "$ns" logs "$name"
kubectl -n "$ns" delete pod "$name" --wait=false >/dev/null
if [ "$phase" != "Succeeded" ] && [ "$phase" != "Failed" ]; then
  echo "incluster-curl: pod $name never finished (phase: ${phase:-unknown})" >&2
  exit 1
fi
