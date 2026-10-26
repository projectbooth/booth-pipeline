"""Chart contract for workload identity and task Jobs (ADR 0056/0057/0058/0096).

These pin the *security topology*, not just the shape: the minting credential reaches the
API/scheduler pod and nothing else; the pod that executes user code — one Job per task, from the
template in the task-job ConfigMap — holds no module credential, no service-account token, and is
reachable only from the API pod. The API pod's Kubernetes access is exactly what creating those Jobs
needs.
"""

from __future__ import annotations

import json

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
    return by_kind(chart, "Deployment")  # the API/scheduler; there is no other


def task_job(items: list[dict]) -> dict:
    """The Job template KubernetesJobRunner creates each task's Job from."""
    return json.loads(by_kind(items, "ConfigMap", "-task-job")["data"]["job.json"])


@pytest.fixture(scope="module")
def task(chart) -> dict:
    return task_job(chart)


def pod(d: dict) -> dict:
    return d["spec"]["template"]["spec"]


def sidecars(p: dict) -> list[dict]:
    """The credential sidecars: every init container but ADR 0115's pip installer (always rendered;
    KubernetesJobRunner removes it from a task without requirements)."""
    return [c for c in p.get("initContainers", []) if c["name"] != "pip"]


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
    assert module["spec"]["events"] == {"subscribe": ["storage.object.created", "api.write.committed"]}  # ADR 0123, nothing more


def test_disabling_workload_identity_omits_the_field_the_secret_and_the_mount():
    items = docs("--set", "workloadIdentity.enabled=false")
    assert "workloadIdentity" not in by_kind(items, "BoothModule", "pipeline")["spec"]
    api = by_kind(items, "Deployment")
    assert "booth-workload-minting-credentials" not in secret_names(api)
    assert "BOOTH_WORKLOAD_MINT_DIR" not in {e["name"] for e in pod(api)["containers"][0]["env"]}


def test_the_minting_credential_is_mounted_into_the_api_pod(api):
    assert "booth-workload-minting-credentials" in secret_names(api)
    c = pod(api)["containers"][0]
    assert {"name": "workload-minting", "mountPath": "/etc/booth/workload", "readOnly": True} in c["volumeMounts"]
    assert {e["name"]: e.get("value") for e in c["env"]}["BOOTH_WORKLOAD_MINT_DIR"] == "/etc/booth/workload"


def test_the_event_bus_credential_is_mounted_into_the_api_pod_only(api, task):
    """ADR 0123: it reads every workspace's Storage and API events, so it is the API pod's alone."""
    assert "booth-event-bus-credentials" in secret_names(api) and "booth-event-bus-credentials" not in secret_names(task)
    c = pod(api)["containers"][0]
    assert {"name": "event-bus", "mountPath": "/etc/booth/event-bus", "readOnly": True} in c["volumeMounts"]
    assert {e["name"]: e.get("value") for e in c["env"]}["BOOTH_PIPELINE_EVENT_BUS_DIR"] == "/etc/booth/event-bus"
    # Optional (ADR 0123 revision 2026-10-22): the pod starts, and schedules run, before core writes it.
    (vol,) = [v for v in pod(api)["volumes"] if v["name"] == "event-bus"]
    assert vol["secret"] == {"secretName": "booth-event-bus-credentials", "optional": True}


def test_disabling_event_triggers_omits_the_events_field_the_secret_and_the_mount():
    items = docs("--set", "eventTriggers.enabled=false")
    assert "events" not in by_kind(items, "BoothModule", "pipeline")["spec"]
    api = by_kind(items, "Deployment")
    assert "booth-event-bus-credentials" not in secret_names(api)
    assert "BOOTH_PIPELINE_EVENT_BUS_DIR" not in {e["name"] for e in pod(api)["containers"][0]["env"]}


def test_the_pod_that_runs_user_code_holds_no_module_credential(task):
    """The heart of ADR 0057. A task pod may read exactly one Secret — its own per-task runner bearer,
    which KubernetesJobRunner names per task — and never the database or minting credentials."""
    (vol,) = [v for v in pod(task)["volumes"] if "secret" in v]
    assert vol["name"] == "runner-auth"
    assert secret_names(task) == {"per-task"}  # a placeholder the runner replaces with "<job>-auth"
    env = {e["name"] for c in pod(task)["containers"] for e in c.get("env", [])}
    assert not {n for n in env if "DSN" in n or "DATABASE" in n or "MINT" in n or "OIDC" in n or "CORE" in n}
    assert not [e for c in pod(task)["containers"] for e in c.get("env", []) if "valueFrom" in e]
    assert not [c for c in pod(task)["containers"] if "envFrom" in c]


def test_a_task_pod_gets_no_service_account_token_and_its_account_has_no_rbac(chart, task):
    spec = pod(task)
    assert spec["automountServiceAccountToken"] is False
    assert spec["serviceAccountName"] == "pipeline-contract-test-booth-pipeline-task"
    sa = by_kind(chart, "ServiceAccount", "-task")
    assert sa["automountServiceAccountToken"] is False
    (binding,) = [d for d in chart if d["kind"] in ("RoleBinding", "ClusterRoleBinding")]
    assert [s["name"] for s in binding["subjects"]] == ["pipeline-contract-test-booth-pipeline"]  # the API pod's, only


def test_a_task_pod_is_one_shot_and_as_locked_down_as_the_api_pod(task):
    spec = pod(task)
    (c,) = spec["containers"]
    assert c["command"] == ["booth-pipeline-runner"]
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["BOOTH_RUNNER_MAX_CONCURRENT"] == "1" and env["BOOTH_RUNNER_ONE_SHOT"] == "1"
    assert env["BOOTH_RUNNER_AUTH_TOKEN_FILE"] == "/etc/booth/runner-auth/token"
    assert c["readinessProbe"]["httpGet"]["path"] == "/healthz"  # what "pod Ready" waits for
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"]["readOnlyRootFilesystem"] is True and c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert {"name": "tmp", "mountPath": "/tmp"} in c["volumeMounts"]  # task working directories need somewhere writable
    assert spec["enableServiceLinks"] is False  # no other Services' addresses in its environment
    assert task["spec"]["ttlSecondsAfterFinished"] == 300
    assert task["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"] == "task"


def test_the_api_pod_may_run_task_jobs_and_nothing_more(chart):
    """docs/decisions/0018's RBAC table: namespace-scoped; no secrets get/list, no pods/log or exec."""
    assert not [d for d in chart if d["kind"] in ("ClusterRole", "ClusterRoleBinding")]
    role = by_kind(chart, "Role", "-task-runner")
    rules = {(g, r): set(rule["verbs"]) for rule in role["rules"] for g in rule["apiGroups"] for r in rule["resources"]}
    assert rules == {
        ("batch", "jobs"): {"create", "get", "list", "watch", "delete"},
        ("", "pods"): {"get", "list", "watch"},
        ("", "secrets"): {"create", "delete"},
    }
    binding = by_kind(chart, "RoleBinding", "-task-runner")
    assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": role["metadata"]["name"]}


def test_the_api_pod_has_a_token_and_is_pointed_at_the_task_job_template(api):
    spec = pod(api)
    assert spec["automountServiceAccountToken"] is True  # the one pod that talks to the Kubernetes API
    c = spec["containers"][0]
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["BOOTH_PIPELINE_TASK_JOB_TEMPLATE"] == "/etc/booth/task-job/job.json"
    assert env["BOOTH_PIPELINE_MAX_CONCURRENT_TASKS"] == "16"
    assert env["BOOTH_PIPELINE_TASK_START_TIMEOUT_SECONDS"] == "300"
    assert env["BOOTH_PIPELINE_TASK_NAME_PREFIX"] == "pipeline-contract-test-booth-pipel-task"  # fullname cut to fit
    assert not {n for n in env if n.startswith("BOOTH_RUNNER_") or n == "BOOTH_PIPELINE_RUNNER_URL"}
    assert {"name": "task-job", "mountPath": "/etc/booth/task-job", "readOnly": True} in c["volumeMounts"]
    assert {"name": "task-job", "configMap": {"name": "pipeline-contract-test-booth-pipeline-task-job"}} in spec["volumes"]
    assert "checksum/task-job" in api["spec"]["template"]["metadata"]["annotations"]


def test_the_task_name_prefix_leaves_room_for_the_job_suffixes():
    """<prefix>-<12 hex>-auth must stay a valid name; Job pod names add a further suffix."""
    items = docs("--set", "fullnameOverride=" + "x" * 63)
    env = {e["name"]: e.get("value") for e in pod(by_kind(items, "Deployment"))["containers"][0]["env"]}
    assert len(env["BOOTH_PIPELINE_TASK_NAME_PREFIX"]) <= 40


def test_there_is_no_shared_runner_any_more(chart):
    assert [d["kind"] for d in chart if d["kind"] in ("Deployment", "StatefulSet", "DaemonSet")] == ["Deployment"]
    assert len([d for d in chart if d["kind"] == "Service"]) == 1
    assert not [d for d in chart if d["kind"] == "Secret"]  # task bearers are created per task, never by the chart


@pytest.mark.parametrize("flag", ["runner.enabled=false", "runner.replicaCount=2", "runner.authSecret.name=mine"])
def test_removed_runner_settings_are_refused_rather_than_silently_ignored(flag):
    out = helm("template", "x", "charts/booth-pipeline", *REQUIRED, "--set", flag)
    assert out.returncode != 0 and "ADR 0096" in out.stderr


def test_only_the_api_pod_can_call_a_task_pod_and_the_database_is_unreachable_from_it(chart):
    np = by_kind(chart, "NetworkPolicy", "-task")
    assert np["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "task"  # every task pod
    assert set(np["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    (rule,) = np["spec"]["ingress"]
    (frm,) = rule["from"]
    # Not "any pod in the namespace" — and so never another task's pod.
    assert frm["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "api"
    assert rule["ports"] == [{"protocol": "TCP", "port": 8080}]
    # egress is an allowlist: DNS and booth-core's gateway. Nothing names a database, and by default the
    # public internet is off too.
    text = yaml.safe_dump(np["spec"]["egress"])
    assert "0.0.0.0/0" not in text and "postgres" not in text.lower()
    assert len(np["spec"]["egress"]) == 2


def test_the_network_policy_selects_exactly_the_pods_the_job_template_makes(chart, task):
    np = by_kind(chart, "NetworkPolicy", "-task")
    labels = task["spec"]["template"]["metadata"]["labels"]
    assert np["spec"]["podSelector"]["matchLabels"].items() <= labels.items()


def test_internet_egress_is_opt_in_and_never_covers_private_ranges():
    np = by_kind(docs("--set", "runner.networkPolicy.egress.allowInternet=true"), "NetworkPolicy", "-task")
    (blk,) = [t["ipBlock"] for r in np["spec"]["egress"] for t in r.get("to", []) if "ipBlock" in t]
    assert blk["cidr"] == "0.0.0.0/0"
    # RFC 1918, shared/CGNAT (100.64.0.0/10) and link-local/metadata, as streamlit, notebooks and spark exclude.
    assert set(blk["except"]) == {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"}


BOOTH_DB_URL = ("--set", "boothDatabase.url=booth-database-postgres.booth-database.svc:5432")


def booth_database_rules(np: dict) -> list[dict]:
    return [r for r in np["spec"]["egress"] for t in r.get("to", []) if t.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/name") == "booth-database"]


def test_booth_database_egress_is_closed_by_default(chart):
    """ADR 0092: booth-database is optional, so with boothDatabase.url empty (the default) nothing
    new opens — task pods stay exactly as closed as ADR 0070 requires."""
    np = by_kind(chart, "NetworkPolicy", "-task")
    assert booth_database_rules(np) == []


def test_setting_booth_database_url_opens_exactly_its_postgres_pod_on_5432():
    np = by_kind(docs(*BOOTH_DB_URL), "NetworkPolicy", "-task")
    (rule,) = booth_database_rules(np)
    (to,) = rule["to"]
    assert to["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "booth-database"}
    assert to["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "booth-database", "app.kubernetes.io/component": "postgres"}
    assert rule["ports"] == [{"protocol": "TCP", "port": 5432}]  # that pod, that port — not the namespace, not any port
    # One rule added to DNS + core; still no internet, still nothing broader than that one pod.
    assert len(np["spec"]["egress"]) == 3
    assert "ipBlock" not in yaml.safe_dump(np["spec"]["egress"])


def test_the_booth_database_selector_follows_the_install():
    np = by_kind(
        docs(*BOOTH_DB_URL, "--set", "runner.networkPolicy.egress.boothDatabase.namespaceSelector.kubernetes\\.io/metadata\\.name=data"),
        "NetworkPolicy",
        "-task",
    )
    (rule,) = booth_database_rules(np)
    assert rule["to"][0]["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "data"}


def test_opening_booth_database_gives_task_pods_no_credential_and_leaves_its_own_database_unreachable():
    """The rule is network reachability only. A task pod still holds no database credential, and this
    module's OWN database (the `database:` values, ADR 0053) is not what the rule selects."""
    items = docs(*BOOTH_DB_URL)
    task = task_job(items)
    assert secret_names(task) == {"per-task"}
    env = {e["name"] for c in pod(task)["containers"] for e in c.get("env", [])}
    assert not {n for n in env if "DSN" in n or "DATABASE" in n}
    np = by_kind(items, "NetworkPolicy", "-task")
    selectors = [t.get("podSelector", {}).get("matchLabels", {}) for r in np["spec"]["egress"] for t in r.get("to", [])]
    assert not [s for s in selectors if s.get("app.kubernetes.io/name") == "booth-pipeline"]  # never this module's pods


def test_the_service_selects_the_api_pod_only(chart):
    assert by_kind(chart, "Service")["spec"]["selector"]["app.kubernetes.io/component"] == "api"


# ---- ADR 0095: the postgres credential sidecar, and the boothStorage.url egress rule -------------

PINNED = "ghcr.io/projectbooth/credential-sidecar@sha256:6a0a795efd27f165e0714beb163d91f5c2feff55cfdc6f287aee979ae02cce14"


def test_no_sidecar_by_default(task):
    assert not sidecars(pod(task))
    env = {e["name"] for e in pod(task)["containers"][0]["env"]}
    assert not {n for n in env if "SIDECAR" in n or n == "DATABASE_URL"}


def test_no_sidecar_without_core_even_with_booth_database():
    """The sidecar calls core's broker; with no core there is only ADR 0092's egress rule."""
    assert not sidecars(pod(task_job(docs(*BOOTH_DB_URL, "--set", "core.url="))))


def test_setting_booth_database_url_adds_a_pinned_native_loopback_sidecar():
    p = pod(task_job(docs(*BOOTH_DB_URL)))
    (sc,) = sidecars(p)
    assert sc["name"] == "credential-sidecar-postgres"
    assert sc["image"] == PINNED  # booth-core@8f0c6b4, never a tag
    assert sc["restartPolicy"] == "Always"  # native: stops with the task, never keeps the Job alive
    assert {"--kind=postgres", "--listen=127.0.0.1:5432", "--token-file=/var/run/booth-sidecar/token"} <= set(sc["args"])
    assert "--core-url=http://booth-core.booth-system.svc:8080" in sc["args"]
    assert not [a for a in sc["args"] if a.startswith(("--workspace", "--scope", "--access", "--token="))]  # per task, by the runner
    assert sc["securityContext"]["readOnlyRootFilesystem"] is True and sc["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert sc["volumeMounts"] == [{"name": "booth-sidecar", "mountPath": "/var/run/booth-sidecar", "readOnly": True}]
    assert not [e for e in sc.get("env", []) if "valueFrom" in e] and "envFrom" not in sc
    assert {"name": "booth-sidecar", "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}} in p["volumes"]
    task_c = p["containers"][0]
    env = {e["name"]: e.get("value") for e in task_c["env"]}
    assert env["BOOTH_RUNNER_SIDECAR_TOKEN_FILE"] == "/var/run/booth-sidecar/token"
    assert "BOOTH_RUNNER_SIDECAR_HEALTHZ" not in env  # which leases to wait for is decided per task, by the runner
    assert "DATABASE_URL" not in env  # it names the workspace's database: added per task by the runner
    assert secret_names(task_job(docs(*BOOTH_DB_URL))) == {"per-task"}  # still no module credential


def test_the_sidecar_image_must_be_pinned_by_digest():
    out = helm("template", "x", "charts/booth-pipeline", *REQUIRED, *BOOTH_DB_URL, "--set", "credentialSidecar.image=ghcr.io/projectbooth/credential-sidecar:latest")
    assert out.returncode != 0 and "pinned by digest" in out.stderr


def test_the_sidecar_reaches_core_and_booth_database_through_the_existing_rules():
    np = by_kind(docs(*BOOTH_DB_URL), "NetworkPolicy", "-task")
    assert len(booth_database_rules(np)) == 1 and len(np["spec"]["egress"]) == 3  # DNS, core, booth-database: nothing new


BOOTH_STORAGE = (
    "--set", "boothStorage.url=minio.e2e-minio.svc:9000",
    "--set", "runner.networkPolicy.egress.boothStorage.namespaceSelector.kubernetes\.io/metadata\.name=e2e-minio",
    "--set", "runner.networkPolicy.egress.boothStorage.podSelector.app=minio",
)  # fmt: skip


def booth_storage_rules(np: dict) -> list[dict]:
    return [r for r in np["spec"]["egress"] for t in r.get("to", []) if t.get("podSelector", {}).get("matchLabels", {}).get("app") == "minio"]


def test_booth_storage_egress_is_closed_by_default(chart):
    np = by_kind(chart, "NetworkPolicy", "-task")
    assert booth_storage_rules(np) == [] and len(np["spec"]["egress"]) == 2


def test_setting_booth_storage_url_opens_exactly_its_backend_on_its_port():
    np = by_kind(docs(*BOOTH_STORAGE), "NetworkPolicy", "-task")
    (rule,) = booth_storage_rules(np)
    (to,) = rule["to"]
    assert to["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "e2e-minio"}
    assert to["podSelector"]["matchLabels"] == {"app": "minio"}
    assert rule["ports"] == [{"protocol": "TCP", "port": 9000}]
    assert len(np["spec"]["egress"]) == 3 and "ipBlock" not in yaml.safe_dump(np["spec"]["egress"])


def test_booth_storage_url_without_selectors_is_refused_rather_than_opened_wide():
    out = helm("template", "x", "charts/booth-pipeline", *REQUIRED, "--set", "boothStorage.url=minio:9000")
    assert out.returncode != 0 and "podSelector are required" in out.stderr


def test_setting_booth_storage_url_adds_a_pinned_native_s3_sidecar():
    """ADR 0095 third amendment: gated on boothStorage.url AND core.url, like postgres."""
    t = task_job(docs(*BOOTH_STORAGE))
    p = pod(t)
    (sc,) = sidecars(p)
    assert sc["name"] == "credential-sidecar-s3" and sc["image"] == PINNED and sc["restartPolicy"] == "Always"
    assert {
        "--kind=s3",
        "--credentials-file=/var/run/booth-sidecar-s3/credentials",
        "--token-file=/var/run/booth-sidecar/token",
        "--core-url=http://booth-core.booth-system.svc:8080",
    } <= set(sc["args"])
    # Never the sidecar's default health listener (127.0.0.1:8080): the runner listens on :8080 in this pod.
    assert "--health-listen=127.0.0.1:8081" in sc["args"]
    assert not [a for a in sc["args"] if a.startswith(("--workspace", "--scope", "--access", "--token="))]  # per task, by the runner
    assert {"name": "booth-sidecar-s3", "mountPath": "/var/run/booth-sidecar-s3"} in sc["volumeMounts"]  # it writes here
    task_c = p["containers"][0]
    env = {e["name"]: e.get("value") for e in task_c["env"]}
    assert env["AWS_SHARED_CREDENTIALS_FILE"] == "/var/run/booth-sidecar-s3/credentials"
    assert env["AWS_CONFIG_FILE"] == "/var/run/booth-sidecar-s3/credentials.config"  # the contract's two-file output
    assert {"name": "booth-sidecar-s3", "mountPath": "/var/run/booth-sidecar-s3", "readOnly": True} in task_c["volumeMounts"]
    assert {"name": "booth-sidecar-s3", "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}} in p["volumes"]
    assert "DATABASE_URL" not in env and secret_names(t) == {"per-task"}


def test_no_s3_sidecar_without_core():
    p = pod(task_job(docs(*BOOTH_STORAGE, "--set", "core.url=")))
    assert not sidecars(p)
    assert not {e["name"] for e in p["containers"][0]["env"]} & {"AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE"}


def test_both_sidecars_share_the_token_volume_and_nothing_else():
    p = pod(task_job(docs(*BOOTH_DB_URL, *BOOTH_STORAGE)))
    pg, s3 = sidecars(p)
    assert (pg["name"], s3["name"]) == ("credential-sidecar-postgres", "credential-sidecar-s3")
    assert {m["name"] for m in pg["volumeMounts"]} == {"booth-sidecar"}  # the postgres one can't touch the s3 files
    assert {m["name"] for m in s3["volumeMounts"]} == {"booth-sidecar", "booth-sidecar-s3"}


def effective_uid(pod_spec: dict, container: dict):
    """What the container actually runs as: its own runAsUser, else the pod's."""
    return container.get("securityContext", {}).get("runAsUser", pod_spec.get("securityContext", {}).get("runAsUser"))


def test_the_s3_sidecar_runs_as_the_same_uid_as_the_task():
    """contracts/credential-sidecar.md: the s3 files are written 0600, owned by the sidecar's uid, so
    the task can read them only as that same uid. Pinned here so a later chart change (a per-container
    runAsUser, a different image default) can't silently break the task's access to its credentials."""
    for values in (BOOTH_STORAGE, (*BOOTH_DB_URL, *BOOTH_STORAGE)):
        p = pod(task_job(docs(*values)))
        task_c = p["containers"][0]
        task_uid = effective_uid(p, task_c)
        assert task_uid == 65532  # set explicitly: the image default must never be what decides this
        for sc in p["initContainers"]:
            assert effective_uid(p, sc) == task_uid, sc["name"]


def test_overriding_the_pod_uid_moves_task_and_sidecar_together():
    p = pod(task_job(docs(*BOOTH_STORAGE, "--set", "podSecurityContext.runAsUser=1234")))
    assert {effective_uid(p, c) for c in (*p["initContainers"], *p["containers"])} == {1234}


# ---- ADR 0115: a task's own Python packages ---------------------------------------------------------

PIP_INDEX = ("--set", "runner.pip.egress.namespaceSelector.kubernetes\\.io/metadata\\.name=pypi", "--set", "runner.pip.egress.podSelector.app=pypi")


def pip_container(p: dict) -> dict:
    (c,) = [c for c in p["initContainers"] if c["name"] == "pip"]
    return c


def api_env(items: list[dict]) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in by_kind(items, "Deployment")["spec"]["template"]["spec"]["containers"][0]["env"]}


def test_the_installer_runs_first_and_mounts_only_the_site_volume():
    p = pod(task_job(docs(*BOOTH_DB_URL, *BOOTH_STORAGE)))
    assert [c["name"] for c in p["initContainers"]] == ["pip", "credential-sidecar-postgres", "credential-sidecar-s3"]
    pip = pip_container(p)
    assert pip["command"] == ["booth-pipeline-pip"] and "restartPolicy" not in pip
    assert pip["volumeMounts"] == [{"name": "site", "mountPath": "/opt/booth/site"}]
    assert {e["name"] for e in pip["env"]} == {"BOOTH_PIP_TARGET", "BOOTH_PIP_TIMEOUT_SECONDS", "BOOTH_PIP_SIZE_LIMIT"}
    assert not any("valueFrom" in e for e in pip["env"]) and "envFrom" not in pip
    assert pip["securityContext"]["readOnlyRootFilesystem"] is True and pip["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert pip["image"] == p["containers"][0]["image"]  # the same interpreter the task runs on


def test_the_task_reads_the_site_volume_read_only():
    p = pod(task_job(docs("--set", "runner.pip.sizeLimit=2Gi")))
    task_c = p["containers"][0]
    (mount,) = [m for m in task_c["volumeMounts"] if m["name"] == "site"]
    assert mount == {"name": "site", "mountPath": "/opt/booth/site", "readOnly": True}
    assert {"name": "BOOTH_RUNNER_SITE_DIR", "value": "/opt/booth/site"} in task_c["env"]
    (vol,) = [v for v in p["volumes"] if v["name"] == "site"]
    assert vol == {"name": "site", "emptyDir": {"sizeLimit": "2Gi"}}  # on disk, not memory


def test_the_index_settings_reach_only_the_installer():
    p = pod(task_job(docs("--set", "runner.pip.indexUrl=http://pypi.pypi.svc:8080/simple", "--set", "runner.pip.trustedHost=pypi.pypi.svc", "--set", "runner.pip.timeoutSeconds=90")))
    env = {e["name"]: e["value"] for e in pip_container(p)["env"]}
    assert env["PIP_INDEX_URL"] == "http://pypi.pypi.svc:8080/simple" and env["PIP_TRUSTED_HOST"] == "pypi.pypi.svc" and env["BOOTH_PIP_TIMEOUT_SECONDS"] == "90"
    assert not {e["name"] for e in p["containers"][0]["env"]} & {"PIP_INDEX_URL", "PIP_TRUSTED_HOST"}


@pytest.mark.parametrize(
    ("args", "route"),
    [
        ((), ""),  # closed and no index: a task with requirements fails at once
        (("--set", "runner.networkPolicy.egress.allowInternet=true"), "internet"),
        (PIP_INDEX, "index"),
        ((*PIP_INDEX, "--set", "runner.networkPolicy.egress.allowInternet=true"), "index"),
        (("--set-json", 'runner.networkPolicy.egress.extra=[{"to":[{"ipBlock":{"cidr":"192.168.1.5/32"}}]}]'), "extra"),
        (("--set", "runner.networkPolicy.enabled=false"), "unrestricted"),
    ],
)
def test_the_api_pod_knows_the_route_to_a_package_index(args, route):
    assert api_env(docs(*args))["BOOTH_PIPELINE_PIP_ROUTE"] == route


def pip_rules(np: dict) -> list[dict]:
    return [r for r in np["spec"]["egress"] if any(t.get("podSelector", {}).get("matchLabels") == {"app": "pypi"} for t in r.get("to", []))]


def test_an_in_cluster_index_opens_exactly_its_pods_on_its_port():
    assert not pip_rules(by_kind(docs(), "NetworkPolicy", "-task"))
    np = by_kind(docs(*PIP_INDEX, "--set", "runner.pip.egress.port=3141"), "NetworkPolicy", "-task")
    (rule,) = pip_rules(np)
    assert rule["to"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "pypi"}}, "podSelector": {"matchLabels": {"app": "pypi"}}}]
    assert rule["ports"] == [{"protocol": "TCP", "port": 3141}]
    assert not any("ipBlock" in t for r in np["spec"]["egress"] for t in r.get("to", []))  # still no internet


def test_an_index_selector_without_the_other_is_refused_rather_than_opened_wide():
    out = helm("template", "x", "charts/booth-pipeline", *REQUIRED, "--set", "runner.pip.egress.podSelector.app=pypi")
    assert out.returncode != 0 and "runner.pip.egress needs both namespaceSelector and podSelector" in out.stderr


# ---- ADR 0119: the Spark runner -------------------------------------------------------------------


def test_the_spark_runners_settings_reach_the_api_pod():
    env = api_env(docs("--set", "runner.spark.retryMinDelaySeconds=200", "--set", "runner.spark.enabled=false"))
    assert env["BOOTH_PIPELINE_SPARK_ENABLED"] == "false"
    assert env["BOOTH_PIPELINE_SPARK_RETRY_MIN_DELAY_SECONDS"] == "200"
    assert env["BOOTH_PIPELINE_SPARK_POLL_SECONDS"] == "5" and env["BOOTH_PIPELINE_SPARK_ADMISSION_WAIT_SECONDS"] == "900"


def test_the_default_retry_floor_is_above_seaweedfs_empty_folder_delay():
    assert int(api_env(docs())["BOOTH_PIPELINE_SPARK_RETRY_MIN_DELAY_SECONDS"]) > 120



# ---- task secrets (ADR 0128) ------------------------------------------------------------------


def test_the_task_secrets_key_is_off_by_default_and_never_in_a_task_pod(chart, api, task):
    assert "booth-pipeline-secrets-key" not in secret_names(api)
    env = {e["name"] for e in pod(api)["containers"][0].get("env", [])}
    assert "BOOTH_PIPELINE_SECRETS_KEY_DIR" not in env


def test_a_named_key_secret_is_mounted_into_the_api_pod_only():
    items = docs("--set", "taskSecrets.keySecretName=booth-pipeline-secrets-key", "--set", "taskSecrets.keyId=k2")
    api = by_kind(items, "Deployment")
    assert "booth-pipeline-secrets-key" in secret_names(api)
    env = {e["name"]: e.get("value") for e in pod(api)["containers"][0]["env"]}
    assert env["BOOTH_PIPELINE_SECRETS_KEY_DIR"] == "/etc/booth/task-secrets-key"
    assert env["BOOTH_PIPELINE_SECRETS_KEY_ID"] == "k2"
    (vol,) = [v for v in pod(api)["volumes"] if v["name"] == "task-secrets-key"]
    assert vol["secret"].get("optional") is not True  # named but missing: the pod waits
    assert "booth-pipeline-secrets-key" not in secret_names(task_job(items))
    assert "booth-pipeline-secrets-key" not in json.dumps(task_job(items))
    # The chart never creates the key: the operator does.
    assert not [d for d in items if d["kind"] == "Secret" and d["metadata"]["name"] == "booth-pipeline-secrets-key"]
