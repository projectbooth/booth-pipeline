"""The s3-mode credential sidecar's scope resolution (ADR 0095, third amendment).

Each task's s3 sidecar is scoped to its workspace's lakehouse warehouse, `{backendId, path}`, which
KubernetesJobRunner asks booth-lakehouse for (GET /api/warehouse through core's gateway, AS THE
TASK). No warehouse (404) means no s3 sidecar, exactly like no platform access."""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from booth_pipeline.runner_service import sidecar_file_path, sidecar_wait
from booth_pipeline.runners.base import Cancellation, TaskAccess, TaskInvocation
from booth_pipeline.runners.kubejob import POSTGRES_SIDECAR, S3_SIDECAR

from .servers import serve_asgi
from .test_kubejob import FakeCluster, Lines, make_runner, template

DIGEST = "ghcr.io/projectbooth/credential-sidecar@sha256:" + "0" * 64
CORE = "http://booth-core.booth-system.svc:8080"
WAREHOUSE = {"workspace": "acme", "backendId": "minio-main", "path": "lakehouse/acme", "warehouseName": "acme", "storageRoot": "s3://b/lakehouse/acme"}


def s3_sidecar() -> dict:
    return {
        "name": S3_SIDECAR,
        "image": DIGEST,
        "restartPolicy": "Always",
        "args": ["--kind=s3", "--credentials-file=/var/run/booth-sidecar-s3/credentials", "--health-listen=127.0.0.1:8081", "--token-file=/var/run/booth-sidecar/token", f"--core-url={CORE}"],
    }


def pg_sidecar() -> dict:
    return {"name": POSTGRES_SIDECAR, "image": DIGEST, "restartPolicy": "Always", "args": ["--kind=postgres", "--listen=127.0.0.1:5432", "--token-file=/var/run/booth-sidecar/token", f"--core-url={CORE}"]}


def tpl(*sidecars: dict) -> dict:
    """What the chart renders with boothStorage.url (and boothDatabase.url) and core.url set."""
    t = template()
    pod = t["spec"]["template"]["spec"]
    pod["initContainers"] = list(sidecars)
    env = ["BOOTH_RUNNER_SIDECAR_TOKEN_FILE", "BOOTH_RUNNER_SIDECAR_WAIT_SECONDS"]
    if any(c["name"] == S3_SIDECAR for c in sidecars):
        env += ["AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE"]
    pod["containers"][0]["env"] = [{"name": n, "value": "x"} for n in env]
    return t


class Lakehouse:
    """booth-lakehouse behind core's gateway, as far as GET /api/warehouse goes."""

    def __init__(self, status: int = 200, body: dict | None = None, fail: Exception | None = None) -> None:
        self.status, self.body, self.fail = status, body if body is not None else WAREHOUSE, fail
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            raise self.fail
        return httpx.Response(self.status, json=self.body if self.status == 200 else {"detail": "no warehouse yet"})


def access(ws: str = "acme", role: str = "editor", token: str = "task-token-acme") -> TaskAccess:
    return TaskAccess(ws, token, "http://s", "http://c", role=role)


def inv(a: TaskAccess | None) -> TaskInvocation:
    return TaskInvocation("run-1", "t", "transform", 1, "pass\n", {}, {}, 60, a)


def personalized(lakehouse: Lakehouse, a: TaskAccess | None, *sidecars: dict) -> tuple[dict, Lines]:
    """The Job a task would get: the warehouse lookup, then the per-task fill-in."""
    r = make_runner(FakeCluster(), tpl(*(sidecars or (s3_sidecar(),))), lakehouse_transport=httpx.MockTransport(lakehouse.handler))
    lines = Lines()
    job = r._job("booth-pipeline-task-abc", inv(a), r._warehouse(inv(a), lines))
    return job["spec"]["template"]["spec"], lines


def env_of(pod: dict) -> dict[str, str]:
    return {e["name"]: e["value"] for e in pod["containers"][0].get("env", [])}


def test_the_scope_is_the_workspaces_warehouse_looked_up_as_the_task():
    lh = Lakehouse()
    pod, lines = personalized(lh, access("acme"))
    (req,) = lh.requests
    assert str(req.url) == f"{CORE}/modules/lakehouse/api/warehouse"  # through core's gateway (ADR 0059)
    assert req.headers["authorization"] == "Bearer task-token-acme"  # the task's own token, nothing more
    assert req.headers["x-workspace"] == "acme"
    (sc,) = pod["initContainers"]
    assert sc["args"][-3:] == ["--workspace=acme", "--scope=" + json.dumps({"backendId": "minio-main", "path": "lakehouse/acme"}), "--access=readwrite"]
    env = env_of(pod)
    assert env["BOOTH_RUNNER_SIDECAR_HEALTHZ"] == "http://127.0.0.1:8081/healthz"
    assert env["AWS_SHARED_CREDENTIALS_FILE"] and env["AWS_CONFIG_FILE"]
    assert lines.out == []


def test_only_backend_and_path_go_into_the_scope():
    """booth-lakehouse returns more (warehouseName, storageRoot, createdBy...); the broker's s3 scope is {backendId, path}."""
    pod, _ = personalized(Lakehouse(), access())
    scope = json.loads(next(a for a in pod["initContainers"][0]["args"] if a.startswith("--scope=")).split("=", 1)[1])
    assert scope == {"backendId": "minio-main", "path": "lakehouse/acme"}


def test_no_warehouse_yet_means_no_s3_sidecar_exactly_like_no_access():
    pod_404, lines = personalized(Lakehouse(status=404), access())
    pod_none, _ = personalized(Lakehouse(), None)
    for pod in (pod_404, pod_none):
        assert pod["initContainers"] == []
        assert not set(env_of(pod)) & {"AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "BOOTH_RUNNER_SIDECAR_HEALTHZ", "BOOTH_RUNNER_SIDECAR_TOKEN_FILE"}
    assert lines.out == []  # an expected state, not an error


def test_a_task_without_access_never_calls_booth_lakehouse():
    lh = Lakehouse()
    personalized(lh, None)
    assert lh.requests == []


def test_no_s3_sidecar_in_the_chart_means_no_lookup():
    lh = Lakehouse()
    pod, _ = personalized(lh, access(), pg_sidecar())
    assert lh.requests == [] and [c["name"] for c in pod["initContainers"]] == [POSTGRES_SIDECAR]


@pytest.mark.parametrize(
    ("lakehouse", "reason"),
    [
        (Lakehouse(status=503), "HTTP 503"),
        (Lakehouse(status=403), "HTTP 403"),
        (Lakehouse(fail=httpx.ConnectError("refused")), "ConnectError"),
        (Lakehouse(body={"unexpected": True}), "KeyError"),
    ],
)
def test_a_failed_lookup_runs_the_task_without_s3_and_says_so(lakehouse, reason):
    """booth-lakehouse being down must not fail a task that may never touch s3."""
    pod, lines = personalized(lakehouse, access())
    assert pod["initContainers"] == []
    (note,) = [m for s, m in lines.out if s == "system"]
    assert reason in note and "without s3 credentials" in note


def test_a_viewer_asks_the_s3_broker_only_to_read():
    pod, _ = personalized(Lakehouse(), access(role="viewer"))
    assert pod["initContainers"][0]["args"][-1] == "--access=read"


def test_with_both_sidecars_the_runner_waits_for_both_leases():
    pod, _ = personalized(Lakehouse(), access(), pg_sidecar(), s3_sidecar())
    assert [c["name"] for c in pod["initContainers"]] == [POSTGRES_SIDECAR, S3_SIDECAR]
    assert env_of(pod)["BOOTH_RUNNER_SIDECAR_HEALTHZ"] == "http://127.0.0.1:5432/healthz,http://127.0.0.1:8081/healthz"


def test_with_both_sidecars_and_no_warehouse_postgres_stays():
    pod, _ = personalized(Lakehouse(status=404), access(), pg_sidecar(), s3_sidecar())
    assert [c["name"] for c in pod["initContainers"]] == [POSTGRES_SIDECAR]
    env = env_of(pod)
    assert env["BOOTH_RUNNER_SIDECAR_HEALTHZ"] == "http://127.0.0.1:5432/healthz" and "DATABASE_URL" in env
    assert not set(env) & {"AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE"}


def test_two_workspaces_get_their_own_warehouses():
    def warehouse_for(req: httpx.Request) -> httpx.Response:
        ws = req.headers["x-workspace"]
        return httpx.Response(200, json={**WAREHOUSE, "workspace": ws, "path": f"lakehouse/{ws}"})

    r = make_runner(FakeCluster(), tpl(s3_sidecar()), lakehouse_transport=httpx.MockTransport(warehouse_for))
    scopes = []
    for ws in ("acme", "globex"):
        i = inv(access(ws, token=f"tok-{ws}"))
        pod = r._job("j", i, r._warehouse(i, Lines()))["spec"]["template"]["spec"]
        scopes.append(next(a for a in pod["initContainers"][0]["args"] if a.startswith("--scope=")))
    assert scopes == ["--scope=" + json.dumps({"backendId": "minio-main", "path": f"lakehouse/{ws}"}) for ws in ("acme", "globex")]


def test_the_lookup_happens_before_the_job_and_its_note_reaches_the_task_log():
    cluster = FakeCluster()
    try:
        r = make_runner(cluster, tpl(s3_sidecar()), lakehouse_transport=httpx.MockTransport(Lakehouse(status=503).handler))
        lines = Lines()
        r.run(inv(access()), lines, Cancellation())
        notes = [m for s, m in lines.out if s == "system"]
        assert "could not look up" in notes[0] and notes[1].startswith("task pod ready")
        created = next(json.loads(b) for q, b in zip(cluster.requests, cluster.bodies, strict=True) if q.method == "POST" and q.url.path.endswith("/jobs"))
        assert created["spec"]["template"]["spec"]["initContainers"] == []
    finally:
        cluster.close()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda sc: sc["args"].__setitem__(2, "--health-listen=127.0.0.1:8080"), "collides with the runner"),
        (lambda sc: sc["args"].__setitem__(2, "--health-listen=0.0.0.0:8081"), "loopback only"),
        (lambda sc: sc["args"].pop(), "needs --core-url"),
        (lambda sc: sc.update(image="ghcr.io/projectbooth/credential-sidecar:latest"), "pinned by digest"),
    ],
)
def test_an_unsafe_s3_sidecar_is_refused_at_startup(mutate, message):
    sc = s3_sidecar()
    mutate(sc)
    with pytest.raises(ValueError, match=message):
        make_runner(FakeCluster(), tpl(sc))


# ---- the one-task runner -----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/var/run/booth-sidecar-s3/credentials", "/var/run/booth-sidecar-s3/credentials.config"])
def test_the_aws_file_variables_may_point_only_at_the_sidecars_files(path):
    assert sidecar_file_path("AWS_SHARED_CREDENTIALS_FILE", path) == path


@pytest.mark.parametrize("path", ["/etc/booth/runner-auth/token", "/var/run/booth-sidecar-s3/../booth-sidecar/token", "AKIA-not-a-path"])
def test_anything_else_in_the_aws_file_variables_is_refused(path):
    with pytest.raises(ValueError, match="must be a file under"):
        sidecar_file_path("AWS_SHARED_CREDENTIALS_FILE", path)


def test_the_runner_waits_for_every_sidecars_lease():
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    ready = {"pg": False, "s3": False}

    def health(name: str) -> FastAPI:
        app = FastAPI()

        @app.get("/healthz")
        def _h():
            return JSONResponse({}, status_code=200 if ready[name] else 503)

        return app

    with serve_asgi(health("pg")) as pg, serve_asgi(health("s3")) as s3:
        threading.Timer(0.3, lambda: ready.update(pg=True)).start()
        threading.Timer(0.8, lambda: ready.update(s3=True)).start()
        lines = Lines()
        t0 = time.monotonic()
        sidecar_wait(f"{pg}/healthz,{s3}/healthz", 10, poll=0.1)(inv(access()), lines)
        assert 0.7 <= time.monotonic() - t0 < 5 and lines.out == []


def test_the_note_names_the_sidecar_still_without_a_lease():
    lines = Lines()
    sidecar_wait("http://127.0.0.1:1/healthz", 0.2, poll=0.1)(inv(access()), lines)
    assert any("the s3 credential sidecar has no lease" in m for _, m in lines.out)
