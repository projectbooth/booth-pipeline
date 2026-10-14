# Shared by egress.sh and pip.sh (sourced, with $repo and $here set): the targets every egress denial
# is checked against, and the works-blocked-works helpers. See egress.sh's header for why each denial
# is a sequence on the same address and port, never a lone "blocked".
source "$here/cni/lan-targets.env"
ns=booth-pipeline
np=booth-pipeline-task
ok=1
lan="lan-10 lan-172 lan-192 lan-cgnat lan-metadata"

step() { echo; echo "=== $*"; }
fail() { echo "FAIL: $*"; exit 1; }

# Not cluster addresses: no pod, Service or node has one (cni/kind-calico.yaml's pool is 10.244.0.0/16).
check_lan_targets() {
  local pool inuse a
  pool=$(kubectl get ippools.crd.projectcalico.org -o jsonpath='{.items[*].spec.cidr}')
  [ "$pool" = 10.244.0.0/16 ] || fail "Calico's pool is '$pool', not 10.244.0.0/16: is Integration on Calico?"
  inuse=$( { kubectl get pods -A -o jsonpath='{.items[*].status.podIP}'; echo
    kubectl get svc -A -o jsonpath='{.items[*].spec.clusterIP}'; echo
    kubectl get nodes -o jsonpath='{.items[*].status.addresses[*].address}'; echo; } | tr ' ' '\n' | sort -u)
  for a in "$LAN_10" "$LAN_172" "$LAN_192" "$LAN_CGNAT" "$LAN_METADATA" "$LAN_CONTROL"; do
    if grep -qxF "$a" <<<"$inuse"; then fail "$a is a cluster address"; fi
  done
}

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

expect() { # RESULT-JSON REGEX PHASE NAME...: each named verdict must match
  local r=$1 re=$2 phase=$3 name got
  shift 3
  for name in "$@"; do
    got=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2], "missing"))' "$r" "$name")
    if grep -Eq "$re" <<<"$got"; then echo "ok: $phase: $name ($got)"; else echo "FAIL: $phase: $name: got '$got', want /$re/"; ok=0; fi
  done
}

# Applied by the chart itself (helm re-creates the deleted object), so the policy under test is exactly
# what the chart renders and helm keeps ownership of it. Calico programs a policy change within about a
# second; each probe is a new task pod created after the change, and the wait makes that margin
# explicit rather than retrying a verdict.
policy_off() { kubectl -n "$ns" delete networkpolicy "$np" --wait=true >/dev/null; sleep 5; echo "(task policy deleted)"; }
policy_on() {
  helm upgrade booth-pipeline "$repo/charts/booth-pipeline" --namespace "$ns" --reuse-values --wait --timeout 5m >/dev/null
  kubectl -n "$ns" get networkpolicy "$np" >/dev/null || fail "helm did not re-create $np"
  sleep 5; echo "(task policy applied by helm)"
}
