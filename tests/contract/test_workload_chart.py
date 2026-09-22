"""Chart contract for workload identity and the separate runner (ADR 0056/0057/0058).

These pin the *security topology*, not just the shape: the minting credential reaches the
API/scheduler pod and nothing else; the pod that executes user code holds no module credential and is
reachable only from the API pod.
"""

from __future__ import annotations

import pytest
import yaml

from .test_manifest import REQUIRED, helm, render


def docs(*extra: str, show_only: str | None = None) -> list[dict]:
    return [d for d in yaml.safe_load_all(render(show_only, *extra)) if d]


def by_kind(items: list[dict], kind: str, name_suffix: str = "") -> dict:
    (found,) = [d for d in items if d["kind"] == kind and d["metadata"]["name"].endswith(name_suffix)]
    return found


@pytest.fixture(scope="module")
def chart() -> list[dict]:
    return docs()


@pytest.fixture(scope="module")
def api(chart) -> dict:
    return by_kind(chart, "Deployment", "booth-pipeline")  # the API/scheduler (the runner's name ends in -runner)


@pytest.fixture(scope="module")
def runner(chart) -> dict:
    return by_kind(chart, "Deployment", "-runner")


def pod(d: dict) -> dict:
    return d["spec"]["template"]["spec"]


def secret_names(d: dict) -> set[str]:
    """Every Secret a pod can read: volumes, env secretKeyRef, envFrom."""
    p = pod(d)
    names = {v["secret"]["secretName"] for v in p.get("volumes", []) if "secret" in v}
    for c in p["containers"]:
        for e in c.get("env", []):
            ref = e.get("valueFrom", {}).get("secretKeyRef")
            if ref:
                names.add(ref["name"])
        for ef in c.get("envFrom", []):
            if "secretRef" in ef:
                names.add(ef["secretRef"]["name"])
    return names


def test_the_manifest_declares_workload_identity_so_core_provisions_a_minting_credential(chart):
    module = by_kind(chart, "BoothModule", "pipeline")
    assert module["spec"]["workloadIdentity"] == {"mint": True}  # module-manifest.md: opt-in, omitted => no credential
    assert "events" not in module["spec"]  # still no bus access


def test_disabling_workload_identity_omits_the_field_the_secret_and_the_mount():
    items = docs("--set", "workloadIdentity.enabled=false")
    assert "workloadIdentity" not in by_kind(items, "BoothModule", "pipeline")["spec"]
    api = by_kind(items, "Deployment", "booth-pipeline")
    assert "booth-workload-minting-credentials" not in secret_names(api)
    assert "BOOTH_WORKLOAD_MINT_DIR" not in {e["name"] for e in pod(api)["containers"][0]["env"]}


def test_the_minting_credential_is_mounted_into_the_api_pod(api):
    assert "booth-workload-minting-credentials" in secret_names(api)
    c = pod(api)["containers"][0]
    assert {"name": "workload-minting", "mountPath": "/etc/booth/workload", "readOnly": True} in c["volumeMounts"]
    assert {e["name"]: e.get("value") for e in c["env"]}["BOOTH_WORKLOAD_MINT_DIR"] == "/etc/booth/workload"


def test_the_pod_that_runs_user_code_holds_no_module_credential(runner):
    """The heart of ADR 0057. The runner may read exactly one Secret — the bearer that lets the API pod
    call it — and never the database credentials or the minting credential."""
    secrets = secret_names(runner)
    assert secrets == {"pipeline-contract-test-booth-pipeline-runner-auth"}
    assert "booth-workload-minting-credentials" not in secrets and "booth-database-credentials" not in secrets
    env = {e["name"] for c in pod(runner)["containers"] for e in c.get("env", [])}
    assert not {n for n in env if "DSN" in n or "DATABASE" in n or "MINT" in n or "OIDC" in n or "CORE" in n}
    assert pod(runner)["automountServiceAccountToken"] is False


def test_the_runner_is_as_locked_down_as_the_api_pod(runner):
    spec = pod(runner)
    c = spec["containers"][0]
    assert c["command"] == ["booth-pipeline-runner"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"]["readOnlyRootFilesystem"] is True and c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert {"name": "tmp", "mountPath": "/tmp"} in c["volumeMounts"]  # task working directories need somewhere writable


def test_the_runners_service_account_has_no_token_and_no_rbac(chart):
    sa = by_kind(chart, "ServiceAccount", "-runner")
    assert sa["automountServiceAccountToken"] is False
    assert not [d for d in chart if d["kind"] in ("Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding")]


def test_only_the_api_pod_can_call_the_runner_and_the_database_is_unreachable_from_it(chart):
    np = by_kind(chart, "NetworkPolicy", "-runner")
    assert set(np["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    (rule,) = np["spec"]["ingress"]
    (frm,) = rule["from"]
    assert frm["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "api"  # not "any pod in the namespace"
    assert rule["ports"] == [{"protocol": "TCP", "port": 8080}]
    # egress is an allowlist: DNS and booth-core's gateway. Nothing names a database, and by default the
    # public internet is off too.
    text = yaml.safe_dump(np["spec"]["egress"])
    assert "0.0.0.0/0" not in text and "postgres" not in text.lower()
    assert len(np["spec"]["egress"]) == 2


def test_internet_egress_is_opt_in_and_never_covers_private_ranges():
    np = by_kind(docs("--set", "runner.networkPolicy.egress.allowInternet=true"), "NetworkPolicy", "-runner")
    (blk,) = [t["ipBlock"] for r in np["spec"]["egress"] for t in r.get("to", []) if "ipBlock" in t]
    assert blk["cidr"] == "0.0.0.0/0" and {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"} <= set(blk["except"])


def test_the_two_services_select_their_own_pods_only(chart):
    api_svc = by_kind(chart, "Service", "booth-pipeline")
    runner_svc = by_kind(chart, "Service", "-runner")
    assert api_svc["spec"]["selector"]["app.kubernetes.io/component"] == "api"
    assert runner_svc["spec"]["selector"]["app.kubernetes.io/component"] == "runner"


def test_the_api_pod_is_told_where_the_runner_is_and_shares_the_bearer_with_it(api, runner):
    env = {e["name"]: e.get("value") for e in pod(api)["containers"][0]["env"]}
    assert env["BOOTH_PIPELINE_RUNNER_URL"] == "http://pipeline-contract-test-booth-pipeline-runner:8080"
    shared = {"pipeline-contract-test-booth-pipeline-runner-auth"}
    assert shared <= secret_names(api) and shared <= secret_names(runner)


def test_the_runner_bearer_is_generated_once_and_can_be_supplied():
    assert by_kind(docs(), "Secret", "-runner-auth")["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    items = docs("--set", "runner.authSecret.name=my-own")
    assert not [d for d in items if d["kind"] == "Secret"]  # ours is not created when you bring your own
    assert "my-own" in secret_names(by_kind(items, "Deployment", "booth-pipeline"))
    assert "my-own" in secret_names(by_kind(items, "Deployment", "-runner"))


def test_more_than_one_runner_replica_is_refused_because_token_refresh_needs_the_same_pod():
    out = helm("template", "x", "charts/booth-pipeline", *REQUIRED, "--set", "runner.replicaCount=2")
    assert out.returncode != 0 and "runner.replicaCount must be 1" in out.stderr


def test_the_minting_credential_and_in_pod_task_execution_cannot_be_combined():
    out = helm("template", "x", "charts/booth-pipeline", *REQUIRED, "--set", "runner.enabled=false")
    assert out.returncode != 0 and "workloadIdentity.enabled requires runner.enabled" in out.stderr


def test_a_dev_install_without_workload_identity_can_still_run_tasks_in_pod():
    items = docs("--set", "runner.enabled=false", "--set", "workloadIdentity.enabled=false")
    assert not [d for d in items if d["metadata"]["name"].endswith("-runner")]
    env = {e["name"] for e in pod(by_kind(items, "Deployment", "booth-pipeline"))["containers"][0]["env"]}
    assert "BOOTH_PIPELINE_RUNNER_URL" not in env


def test_the_runner_never_rolls_two_pods_at_once(runner):
    assert runner["spec"]["strategy"]["type"] == "Recreate"
