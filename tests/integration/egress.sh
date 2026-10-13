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
#     192.168.0.0/16 and the metadata address 169.254.169.254 (cni/lan-targets.sh makes each answer);
#     allowed: DNS.
#   allowInternet=true:
#     denied: everything above except the internet; allowed: the internet and a LAN-path control
#     address outside every excluded range (it reaches the same host listener the LAN hosts do).
#
# 100.64.0.0/10 (CGNAT) is probed and reported, not asserted: the chart's allowInternet rule does
# not exclude it, and no ADR says it must.
#
#   egress.sh REPO-ROOT
set -euo pipefail
repo=${1:-.}
here="$repo/tests/integration"
source "$here/cni/lan-targets.env"
ns=booth-pipeline
np=booth-pipeline-task
ok=1

step() { echo; echo "=== $*"; }
fail() { echo "FAIL: $*"; exit 1; }

# Not cluster addresses: no pod, Service or node has one (cni/kind-calico.yaml's pool is 10.244.0.0/16).
pool=$(kubectl get ippools.crd.projectcalico.org -o jsonpath='{.items[*].spec.cidr}')
[ "$pool" = 10.244.0.0/16 ] || fail "Calico's pool is '$pool', not 10.244.0.0/16: is Integration on Calico?"
inuse=$( { kubectl get pods -A -o jsonpath='{.items[*].status.podIP}'; echo
  kubectl get svc -A -o jsonpath='{.items[*].spec.clusterIP}'; echo
  kubectl get nodes -o jsonpath='{.items[*].status.addresses[*].address}'; echo; } | tr ' ' '\n' | sort -u)
for a in "$LAN_10" "$LAN_172" "$LAN_192" "$LAN_CGNAT" "$LAN_METADATA" "$LAN_CONTROL"; do
  if grep -qxF "$a" <<<"$inuse"; then fail "$a is a cluster address"; fi
done

targets() { # name=host:port pairs, by address so no DNS is involved in the verdicts
  local db api kube node
  db=$(kubectl -n "$ns" get endpoints postgres -o jsonpath='{.subsets[0].addresses[0].ip}')
  api=$(kubectl -n "$ns" get pod -l app.kubernetes.io/component=api --field-selector=status.phase=Running -o jsonpath='{.items[0].status.podIP}')
  kube=$(kubectl -n default get svc kubernetes -o jsonpath='{.spec.clusterIP}')
  node=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
  for v in db api kube node; do [ -n "${!v}" ] || fail "no address for $v"; done
  echo "own-database=$db:5432 api-pod=$api:8080 kubernetes-api=$kube:443 kubelet=$node:10250 internet=1.1.1.1:443" \
    "lan-10=$LAN_10:$LAN_PORT lan-172=$LAN_172:$LAN_PORT lan-192=$LAN_192:$LAN_PORT lan-metadata=$LAN_METADATA:$LAN_PORT" \
    "lan-control=$LAN_CONTROL:$LAN_PORT lan-cgnat=$LAN_CGNAT:$LAN_PORT"
}

probe() { # TARGETS...: one fresh task pod's verdicts, as JSON
  kubectl -n "$ns" exec -i deploy/booth-pipeline -- python - egress "$@" <"$here/task_jobs_driver.py" | tail -1
}

expect() { # RESULT-JSON REGEX PHASE NAME...: each named verdict must match
  local r=$1 re=$2 phase=$3 name got
  shift 3
  for name in "$@"; do
    got=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2], "missing"))' "$r" "$name")
    if grep -Eq "$re" <<<"$got"; then echo "ok: $phase: $name ($got)"; else echo "FAIL: $phase: $name: got '$got', want /$re/"; ok=0; fi
  done
}

policy_save() {
  kubectl -n "$ns" get networkpolicy "$np" -o json | python3 -c '
import json, sys
o = json.load(sys.stdin); m = o["metadata"]
keep = {k: m[k] for k in ("name", "namespace", "labels", "annotations") if k in m}
keep.get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
print(json.dumps({"apiVersion": o["apiVersion"], "kind": o["kind"], "metadata": keep, "spec": o["spec"]}))' >/tmp/task-np.json
}
# Calico programs a policy change within about a second; each probe is a new task pod created after
# the change, and the wait makes that margin explicit rather than retrying a verdict.
policy_off() { kubectl -n "$ns" delete networkpolicy "$np" --wait=true >/dev/null; sleep 5; echo "(task policy deleted)"; }
policy_on() { kubectl -n "$ns" apply -f /tmp/task-np.json >/dev/null; sleep 5; echo "(task policy applied)"; }

sequence() { # MODE "DENIED..." "ALLOWED...": off -> on -> off on the same targets, then the policy restored
  local mode=$1 denied=$2 allowed=$3 all r tl
  all="$denied $allowed"
  policy_save
  tl=$(targets) || exit 1
  read -r -a t <<<"$tl"
  echo "targets: ${t[*]}"

  policy_off
  r=$(probe "${t[@]}"); echo "no policy: $r"
  # shellcheck disable=SC2086
  expect "$r" '^open$' "$mode, policy deleted (positive control)" $all
  expect "$r" '.' "$mode, policy deleted (reported only)" lan-cgnat

  policy_on
  r=$(probe "${t[@]}"); echo "policy: $r"
  # shellcheck disable=SC2086
  expect "$r" '^(timeout|refused)$' "$mode, policy applied: denied" $denied
  # shellcheck disable=SC2086
  [ -z "$allowed" ] || expect "$r" '^open$' "$mode, policy applied: allowed" $allowed
  expect "$r" '^resolves$' "$mode, policy applied: allowed" dns
  expect "$r" '.' "$mode, policy applied (reported only)" lan-cgnat

  policy_off
  r=$(probe "${t[@]}"); echo "no policy again: $r"
  # shellcheck disable=SC2086
  expect "$r" '^open$' "$mode, policy deleted again (positive control)" $all

  policy_on  # restore what the chart installed
  [ "$ok" = 1 ] || fail "$mode: see the FAIL lines above"
}

lan="lan-10 lan-172 lan-192 lan-metadata"

step "closed (allowInternet off, the default): every destination but DNS is blocked, each with works-blocked-works"
grep -q "ipBlock" <<<"$(kubectl -n "$ns" get networkpolicy "$np" -o yaml)" && fail "the default task policy has an ipBlock rule"
sequence closed "own-database api-pod kubernetes-api kubelet internet $lan lan-control" ""

step "allowInternet=true: the internet opens; this module's database, the cluster and LAN ranges stay blocked"
helm upgrade booth-pipeline "$repo/charts/booth-pipeline" --namespace "$ns" --reuse-values \
  --set runner.networkPolicy.egress.allowInternet=true --wait --timeout 5m >/dev/null
kubectl -n "$ns" rollout status deploy/booth-pipeline --timeout=180s
grep -q "0.0.0.0/0" <<<"$(kubectl -n "$ns" get networkpolicy "$np" -o yaml)" || fail "allowInternet did not add the internet rule"
sequence allowInternet "own-database api-pod kubernetes-api kubelet $lan" "internet lan-control"

step "back to closed for the steps that follow"
helm upgrade booth-pipeline "$repo/charts/booth-pipeline" --namespace "$ns" --reuse-values \
  --set runner.networkPolicy.egress.allowInternet=false --wait --timeout 5m >/dev/null
kubectl -n "$ns" rollout status deploy/booth-pipeline --timeout=180s
grep -q "ipBlock" <<<"$(kubectl -n "$ns" get networkpolicy "$np" -o yaml)" && fail "the internet rule is still there"
echo
echo "egress: OK — every denial held as works-blocked-works on the same target, on Calico"
