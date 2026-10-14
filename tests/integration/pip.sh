#!/usr/bin/env bash
# A task's own Python packages (ADR 0115 and its amendment 1; docs/design-task-requirements.md), on
# real task pods with Calico enforcing the task NetworkPolicy. Runs after the credential-sidecar steps,
# so core.url and boothDatabase.url are set and a task with platform access gets the postgres sidecar.
#
# The index is in-cluster (fixtures/pypi/): a PEP 503 index of the real requests/beautifulsoup4
# wheels (hash-locked in wheels.lock) and the test's own packages. booth-pip-probe is an sdist whose
# BUILD runs in the pip init container and reports what that container can reach and see.
#
#   0. No route to any index (allowInternet off, no runner.pip.egress): a task with requirements fails
#      at once, naming the value to set, and no Job is created.
#   1. The installer reaches only the index: from the init container AND the task's own code, every
#      egress.sh target is works-blocked-works on the same address; the index stays open throughout.
#   2. The installer holds no token or credentials: its mounts, environment and the sidecars' ports,
#      and a file and process scan that must find a planted decoy but neither the runner bearer nor the
#      platform token. The same pod's task then does hold its token and reaches its database.
#   3. A failed install fails the task, with pip's own reason: an sdist whose build fails, and a
#      download that trickles (only the installer's whole-install timeout can stop it).
#   4. A requirement that isn't available fails clearly, and is not blamed on the network.
#   5. The first real case: requests and beautifulsoup4 with allowInternet=true, from the in-cluster
#      index, imported from the installed site and used to fetch and parse a page.
#
#   pip.sh REPO-ROOT
set -euo pipefail
repo=${1:-.}
here="$repo/tests/integration"
source "$here/egress-lib.sh"

index_host=pypi.pypi-ci.svc.cluster.local
index_url="http://$index_host:8080/simple"
summary=${GITHUB_STEP_SUMMARY:-/dev/null}

drv() { kubectl -n "$ns" exec -i deploy/booth-pipeline -- python - "$@" <"$here/task_jobs_driver.py"; }
route() { kubectl -n "$ns" get deploy booth-pipeline -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="BOOTH_PIPELINE_PIP_ROUTE")].value}'; }
check() { # JSON JQ-EXPRESSION WHAT
  if jq -e "$2" >/dev/null <<<"$1"; then echo "ok: $3"; else echo "FAIL: $3 ($2)"; ok=0; fi
}
done_ok() { [ "$ok" = 1 ] || fail "$1: see the FAIL lines above"; }
no_task_jobs() { [ -z "$(kubectl -n "$ns" get jobs -l booth.projectbooth.io/task-job -o name)" ]; }

step "0. no route to a package index: a task with requirements fails at once, naming the value to set"
[ -z "$(route)" ] || fail "expected no pip route before runner.pip is set, got '$(route)'"
for _ in $(seq 1 60); do no_task_jobs && break; sleep 1; done
no_task_jobs || fail "task Jobs from earlier steps are still there"
r=$(drv pip-run requests); echo "$r"
check "$r" '.error == "this install has no route to a package index: set runner.pip.egress for an in-cluster index, or runner.networkPolicy.egress.allowInternet=true for PyPI (ADR 0115)"' "the task failed with the value to set"
check "$r" '[.system[] | select(startswith("task pod ready"))] | length == 0' "no task pod was started"
check "$r" '.seconds < 5' "it failed at once"
no_task_jobs || fail "a Job was created for a task that could not install its packages"
done_ok "no route"

step "the in-cluster test index, and runner.pip pointing at it"
kubectl apply -f "$here/fixtures/pypi/pypi.yaml"
kubectl -n pypi-ci rollout status deploy/pypi --timeout=120s
helm upgrade booth-pipeline "$repo/charts/booth-pipeline" --namespace "$ns" --reuse-values \
  --set runner.pip.indexUrl="$index_url" --set runner.pip.trustedHost="$index_host" \
  --set runner.pip.egress.namespaceSelector."kubernetes\.io/metadata\.name"=pypi-ci \
  --set runner.pip.egress.podSelector.app=pypi-ci \
  --set runner.pip.timeoutSeconds=30 \
  --wait --timeout 5m >/dev/null
kubectl -n "$ns" rollout status deploy/booth-pipeline --timeout=180s
api_pod_ip >/dev/null  # the old pod is gone, so exec and targets reach the new one
[ "$(route)" = index ] || fail "expected the pip route 'index', got '$(route)'"
grep -q "ipBlock" <<<"$(kubectl -n "$ns" get networkpolicy "$np" -o yaml)" && fail "allowInternet is on: proof 1 needs it off"

step "1. the installer reaches only the index: init container and task code, works-blocked-works on every target"
check_lan_targets
index_ip=$(kubectl -n pypi-ci get pod -l app=pypi-ci -o jsonpath='{.items[0].status.podIP}')
read -r -a t <<<"$(targets) index=$index_ip:8080"
echo "targets: ${t[*]}"
python3 -c 'import json,sys; print(json.dumps(dict(p.split("=", 1) for p in sys.argv[1:])))' "${t[@]}" \
  | kubectl -n pypi-ci exec -i deploy/pypi -- sh -c 'cat > /tmp/probe-targets.json'
denied="own-database api-pod kubernetes-api kubelet internet $lan lan-control"
both() { local n; for n in "$@"; do printf 'init:%s task:%s ' "$n" "$n"; done; }
policy_off
r=$(drv pip-probe "${t[@]}" | tail -1); echo "no policy: $r"
# shellcheck disable=SC2046
expect "$r" '^open$' "policy deleted (positive control)" $(both $denied index)
policy_on
r=$(drv pip-probe "${t[@]}" | tail -1); echo "policy: $r"
# shellcheck disable=SC2046
expect "$r" '^(timeout|refused)$' "policy applied: denied" $(both $denied)
expect "$r" '^open$' "policy applied: the index" init:index task:index
policy_off
r=$(drv pip-probe "${t[@]}" | tail -1); echo "no policy again: $r"
# shellcheck disable=SC2046
expect "$r" '^open$' "policy deleted again (positive control)" $(both $denied index)
policy_on
done_ok "proof 1"

step "2. the installer holds no token or credentials, in a task pod that has both"
drv pip-secrets acme editor || fail "proof 2: see the checks above"

step "3. a failed install fails the task with pip's own reason"
r=$(drv pip-run booth-pip-broken); echo "$r"
check "$r" '.error | startswith("pip install failed:\n")' "pip install failed"
check "$r" '.error | contains("BOOTH-PIP-BROKEN: this sdist'"'"'s build fails on purpose")' "pip's account of THIS build"
check "$r" '.error | contains("could not reach") | not' "not blamed on the network"
check "$r" '.stdout | index("TASK-CODE-RAN") | not' "the task's code never ran"
check "$r" '.system | index("installing 1 Python requirement(s) before the task starts: booth-pip-broken") != null' "the log says what it installs"
r=$(drv pip-run booth-pip-trickle); echo "$r"
check "$r" '.error == "pip install timed out after 30s (runner.pip.timeoutSeconds)"' "a trickling download is killed at the whole-install timeout"
check "$r" '.seconds < 75' "within the timeout plus a pod start"
done_ok "proof 3"

step "4. a requirement that isn't available fails clearly"
for req in booth-no-such-package==1.0 requests==0.0.1; do
  r=$(drv pip-run "$req"); echo "$r"
  check "$r" ".error | startswith(\"pip install failed:\n\") and contains(\"No matching distribution found for $req\")" "$req: pip's own reason"
  check "$r" '.error | contains("could not reach") | not' "$req: not blamed on the network"
done
done_ok "proof 4"

step "5. requests and beautifulsoup4, with allowInternet=true, from the in-cluster index"
helm upgrade booth-pipeline "$repo/charts/booth-pipeline" --namespace "$ns" --reuse-values \
  --set runner.networkPolicy.egress.allowInternet=true --wait --timeout 5m >/dev/null
kubectl -n "$ns" rollout status deploy/booth-pipeline --timeout=180s
api_pod_ip >/dev/null  # the old pod is gone, so exec and targets reach the new one
grep -q "0.0.0.0/0" <<<"$(kubectl -n "$ns" get networkpolicy "$np" -o yaml)" || fail "allowInternet did not add the internet rule"
r=$(drv pip-scrape "http://$index_host:8080/page.html"); echo "$r"
check "$r" '.error == null' "the task succeeded"
check "$r" '.output.team == "Booth Bears" and .output.scores == [21, 17]' "it fetched and parsed the page"
check "$r" '.output.requests[0] == "2.34.2" and (.output.requests[1] | startswith("/opt/booth/site/"))' "requests is the installed one"
check "$r" '.output.bs4[0] == "4.15.0" and (.output.bs4[1] | startswith("/opt/booth/site/"))' "beautifulsoup4 is the installed one"
check "$r" '.system | index("installing 2 Python requirement(s) before the task starts: requests, beautifulsoup4") != null' "the log says what it installs"
check "$r" '[.system[] | select(startswith("pip: Successfully installed"))] | length == 1' "pip's own output is in the task's log"
check "$r" '[.system[] | select(test("^packages installed in [0-9.]+s"))] | length == 1' "and how long it took"
{
  echo '### A task with requests and beautifulsoup4 (ADR 0115), allowInternet=true, from the in-cluster index'
  echo '```'
  jq -r '.system[]' <<<"$r"
  echo "task total: $(jq -r .seconds <<<"$r")s (Job created, install, pod Ready, run)"
  echo '```'
} >>"$summary"
helm upgrade booth-pipeline "$repo/charts/booth-pipeline" --namespace "$ns" --reuse-values \
  --set runner.networkPolicy.egress.allowInternet=false --wait --timeout 5m >/dev/null
done_ok "proof 5"

step "every task above left no Job, pod or Secret behind"
left=""
for _ in $(seq 1 60); do
  left="$(kubectl -n "$ns" get jobs,pods,secrets -l booth.projectbooth.io/task-job -o name)"
  [ -z "$left" ] && break
  sleep 1
done
[ -z "$left" ] || fail "left behind: $left"
echo
echo "pip: OK"
