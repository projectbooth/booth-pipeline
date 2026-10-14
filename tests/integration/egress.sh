#!/usr/bin/env bash
# Task pod egress (ADR 0057, re-targeted onto task Jobs by ADR 0096; charts/booth-pipeline/templates/
# task-networkpolicy.yaml), probed from inside real task pods, where task code runs
# (task_jobs_driver.py's `egress` mode). ARCHITECTURE.md item 60.
#
# Needs a CNI that enforces egress NetworkPolicy: Integration runs Calico (cni/), because kind's
# default kindnet does not reliably enforce egress, which could make a denial below vacuous.
#
# Every denial is the SAME sequence on the SAME address and port, so a "blocked" can only be the
# policy: reachable with the task policy deleted, blocked while it is applied, reachable again once
# it is deleted. A timeout and a refusal are equally valid "blocked" outcomes (Calico drops; k3s's
# kube-router answers "refused"); a refusal is never accepted without the sequence around it.
#
#   closed (the default: allowInternet off; no core.url, boothDatabase.url or boothStorage.url here):
#     denied: this module's own PostgreSQL, the API pod, the Kubernetes API (Service ClusterIP), the
#     node's kubelet, the internet, and a LAN host in each of 10.0.0.0/8, 172.16.0.0/12,
#     192.168.0.0/16, 100.64.0.0/10 (CGNAT) and the metadata address 169.254.169.254
#     (cni/lan-targets.sh makes each answer);
#     allowed: DNS.
#   allowInternet=true:
#     denied: everything above except the internet; allowed: the internet and a LAN-path control
#     address outside every excluded range (it reaches the same host listener the LAN hosts do).
#
#   egress.sh REPO-ROOT
set -euo pipefail
repo=${1:-.}
here="$repo/tests/integration"
source "$here/egress-lib.sh"
check_lan_targets

probe() { # TARGETS...: one fresh task pod's verdicts, as JSON
  kubectl -n "$ns" exec -i deploy/booth-pipeline -- python - egress "$@" <"$here/task_jobs_driver.py" | tail -1
}

sequence() { # MODE "DENIED..." "ALLOWED...": off -> on -> off on the same targets, then the policy restored
  local mode=$1 denied=$2 allowed=$3 all r tl
  all="$denied $allowed"
  tl=$(targets) || exit 1
  read -r -a t <<<"$tl"
  echo "targets: ${t[*]}"

  policy_off
  r=$(probe "${t[@]}"); echo "no policy: $r"
  # shellcheck disable=SC2086
  expect "$r" '^open$' "$mode, policy deleted (positive control)" $all

  policy_on
  r=$(probe "${t[@]}"); echo "policy: $r"
  # shellcheck disable=SC2086
  expect "$r" '^(timeout|refused)$' "$mode, policy applied: denied" $denied
  # shellcheck disable=SC2086
  [ -z "$allowed" ] || expect "$r" '^open$' "$mode, policy applied: allowed" $allowed
  expect "$r" '^resolves$' "$mode, policy applied: allowed" dns

  policy_off
  r=$(probe "${t[@]}"); echo "no policy again: $r"
  # shellcheck disable=SC2086
  expect "$r" '^open$' "$mode, policy deleted again (positive control)" $all

  policy_on  # restore what the chart installed
  [ "$ok" = 1 ] || fail "$mode: see the FAIL lines above"
}

step "closed (allowInternet off, the default): every destination but DNS is blocked, each with works-blocked-works"
grep -q "ipBlock" <<<"$(kubectl -n "$ns" get networkpolicy "$np" -o yaml)" && fail "the default task policy has an ipBlock rule"
sequence closed "own-database api-pod kubernetes-api kubelet internet $lan lan-control" ""

step "allowInternet=true: the internet opens; this module's database, the cluster and LAN ranges stay blocked"
helm upgrade booth-pipeline "$repo/charts/booth-pipeline" --namespace "$ns" --reuse-values \
  --set runner.networkPolicy.egress.allowInternet=true --wait --timeout 5m >/dev/null
kubectl -n "$ns" rollout status deploy/booth-pipeline --timeout=180s
api_pod_ip >/dev/null  # the old pod is gone, so exec and targets reach the new one
grep -q "0.0.0.0/0" <<<"$(kubectl -n "$ns" get networkpolicy "$np" -o yaml)" || fail "allowInternet did not add the internet rule"
sequence allowInternet "own-database api-pod kubernetes-api kubelet $lan" "internet lan-control"

step "back to closed for the steps that follow"
helm upgrade booth-pipeline "$repo/charts/booth-pipeline" --namespace "$ns" --reuse-values \
  --set runner.networkPolicy.egress.allowInternet=false --wait --timeout 5m >/dev/null
kubectl -n "$ns" rollout status deploy/booth-pipeline --timeout=180s
api_pod_ip >/dev/null  # the old pod is gone, so exec and targets reach the new one
grep -q "ipBlock" <<<"$(kubectl -n "$ns" get networkpolicy "$np" -o yaml)" && fail "the internet rule is still there"
echo
echo "egress: OK — every denial held as works-blocked-works on the same target, on Calico"
