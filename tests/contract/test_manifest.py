"""Contract tests (testing-strategy.md layer 2): validate this module's manifest — the BoothModule
custom resource its Helm chart templates — against contracts/module-manifest.md, and check the
chart's security posture. Runs against a *rendered template* (``helm template``), no cluster; a
``helm`` binary is needed, which CI installs. Skips (locally only) when helm is absent; CI sets
BOOTH_TEST_REQUIRE_EMULATORS=1 so a missing helm FAILS the build instead of skipping green.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "charts" / "booth-pipeline"
REQUIRED = ["--set", "oidc.issuerUrl=https://keycloak.example.com/realms/booth", "--set", "oidc.clientId=booth-pipeline"]


def helm(*args: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("helm") is None:
        if os.environ.get("BOOTH_TEST_REQUIRE_EMULATORS") == "1":
            pytest.fail("helm is not installed but BOOTH_TEST_REQUIRE_EMULATORS=1")
        pytest.skip("helm not installed; CI has it")
    return subprocess.run(["helm", *args], capture_output=True, text=True, check=False)


def render(show_only: str | None = None, *extra: str, required: bool = True) -> str:
    args = ["template", "pipeline-contract-test", str(CHART), "--namespace", "booth-pipeline"]
    if required:
        args += REQUIRED
    args += list(extra)
    if show_only:
        args += ["--show-only", show_only]
    out = helm(*args)
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.fixture(scope="module")
def module() -> dict:
    return yaml.safe_load(render("templates/boothmodule.yaml"))


@pytest.fixture(scope="module")
def deployment() -> dict:
    return yaml.safe_load(render("templates/deployment.yaml"))


def test_manifest_required_fields(module):
    assert (module["apiVersion"], module["kind"]) == ("booth.projectbooth.io/v1alpha1", "BoothModule")  # ADR 0019
    s = module["spec"]
    assert s["id"] == "pipeline"  # "matches the repo name minus booth-"
    assert re.fullmatch(r"[a-z][a-z0-9-]*", s["id"])  # booth-core's CRD validation
    assert s["displayName"]
    assert re.match(r"^\d+\.\d+\.\d+", s["version"]) and re.match(r"^\d+\.\d+\.\d+", s["contractVersion"])
    assert s["healthCheckPath"].startswith("/")
    assert s["serviceRef"]["name"] and s["serviceRef"]["port"] == 8080


def test_manifest_ui_rules(module):
    s = module["spec"]
    assert s["hasOwnUi"] is True
    assert s["uiIntegrationMode"] == "native"  # ui-integration.md: our own React builder, not a wrapped third-party UI
    assert s["navGroup"] == "build"  # ADR 0017 / the brief
    assert s["navPath"] == "/pipeline"
    assert "adminNavPath" not in s  # one view for every role; controls are role-gated inside it


def test_manifest_declares_its_database_and_no_event_bus_access(module):
    s = module["spec"]
    # ADR 0053: persistence needs this, or core provisions nothing and there is no DSN to read.
    assert s["database"] == {"enabled": True}
    # ADR 0050: v0 neither publishes nor subscribes, and undeclared means no credential — least
    # privilege. If run events are added later this assertion should change WITH that decision.
    assert "events" not in s


def test_required_scopes_are_declared(module):
    assert set(module["spec"]["requiredScopes"]) == {"pipeline.read", "pipeline.write"}


def test_health_path_matches_the_readiness_probe_and_liveness_ignores_the_database(module, deployment):
    c = deployment["spec"]["template"]["spec"]["containers"][0]
    assert c["readinessProbe"]["httpGet"]["path"] == module["spec"]["healthCheckPath"]
    assert c["livenessProbe"]["httpGet"]["path"] == "/livez"  # restarting the pod cannot fix a Postgres outage


def test_the_database_dsn_comes_from_the_core_provisioned_secret(deployment):
    env = {e["name"]: e for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    ref = env["BOOTH_PIPELINE_DATABASE_DSN"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "booth-database-credentials", "key": "dsn"}  # core-platform-api.md, ADR 0053


def test_the_role_is_rederived_from_the_token_so_the_groups_claim_reaches_the_pod(deployment):
    env = {e["name"]: e.get("value") for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["BOOTH_OIDC_GROUPS_CLAIM"] == "groups"  # booth-core's default (ADR 0025/0041)
    other = yaml.safe_load(render("templates/deployment.yaml", "--set", "oidc.groupsClaim=memberships"))
    env2 = {e["name"]: e.get("value") for e in other["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env2["BOOTH_OIDC_GROUPS_CLAIM"] == "memberships"


def test_no_kubernetes_api_access_and_no_mounted_token():
    """Pipeline tasks are user code running in this pod, so anything mounted is reachable by
    them. The module never calls the Kubernetes API: no RBAC, no service-account token."""
    everything = render()
    for kind in ("kind: Role", "kind: ClusterRole", "kind: RoleBinding", "kind: ClusterRoleBinding"):
        assert kind not in everything, f"chart renders {kind}; booth-pipeline needs no Kubernetes API access"
    assert yaml.safe_load(render("templates/deployment.yaml"))["spec"]["template"]["spec"]["automountServiceAccountToken"] is False


def test_the_pod_is_locked_down_but_has_a_writable_scratch_dir(deployment):
    spec = deployment["spec"]["template"]["spec"]
    c = spec["containers"][0]
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    # tasks run as subprocesses with a working directory in $TMPDIR: without a writable /tmp on a
    # read-only root filesystem every single task would fail to start
    assert {"name": "tmp", "mountPath": "/tmp"} in c["volumeMounts"]
    assert any(v["name"] == "tmp" and "emptyDir" in v for v in spec["volumes"])


def test_oidc_settings_are_required_not_silently_empty():
    out = helm("template", "x", str(CHART))
    assert out.returncode != 0 and "oidc.issuerUrl is required" in out.stderr


def test_the_catalog_is_optional_inline_code_needs_no_other_module():
    """The brief's hard requirement, at chart level: the base runner works with zero other modules,
    so pointing at core is a convenience that can be switched off, never a prerequisite."""
    dep = yaml.safe_load(render("templates/deployment.yaml", "--set", "core.url="))
    env = {e["name"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "BOOTH_CORE_URL" not in env


def test_runner_env_passthrough_is_opt_in_and_empty_by_default(deployment):
    env = {e["name"] for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "BOOTH_PIPELINE_RUNNER_ENV_PASSTHROUGH" not in env
    dep = yaml.safe_load(render("templates/deployment.yaml", "--set", "execution.runnerEnvPassthrough={HTTPS_PROXY,NO_PROXY}"))
    e2 = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert e2["BOOTH_PIPELINE_RUNNER_ENV_PASSTHROUGH"] == "HTTPS_PROXY,NO_PROXY"


def test_helm_lint_is_clean():
    out = helm("lint", str(CHART), *REQUIRED)
    assert out.returncode == 0, out.stdout + out.stderr
