"""The credential sidecar's wiring (ADR 0095, simplified by ADR 0096): how each task's Job is
personalized for it, and how the one-task runner keeps its token where the sidecar reads it.

The sidecar itself (booth-core's binary) against a real broker and a real Postgres, and the
NetworkPolicy that gates it, are exercised on a real cluster in .github/workflows/integration.yml."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time

import pytest

from booth_pipeline.runner_service import create_runner_app, loopback_database_url, sidecar_wait
from booth_pipeline.runners.base import Cancellation, TaskAccess, TaskInvocation
from booth_pipeline.runners.kubejob import POSTGRES_SIDECAR, KubernetesJobRunner, workspace_database
from booth_pipeline.runners.remote import RemoteRunner
from booth_pipeline.runners.subprocess_runner import SubprocessRunner

from .servers import serve_asgi
from .test_kubejob import FakeCluster, Lines, make_runner, template

DIGEST = "ghcr.io/projectbooth/credential-sidecar@sha256:" + "0" * 64
SIDECAR_ENV = ["BOOTH_RUNNER_SIDECAR_TOKEN_FILE", "BOOTH_RUNNER_SIDECAR_HEALTHZ", "BOOTH_RUNNER_SIDECAR_WAIT_SECONDS"]


def sidecar_template() -> dict:
    """What the chart renders when boothDatabase.url and core.url are both set."""
    t = template()
    pod = t["spec"]["template"]["spec"]
    pod["initContainers"] = [
        {
            "name": POSTGRES_SIDECAR,
            "image": DIGEST,
            "restartPolicy": "Always",
            "args": ["--kind=postgres", "--listen=127.0.0.1:5432", "--token-file=/var/run/booth-sidecar/token", "--core-url=http://core"],
        }
    ]
    pod["containers"][0]["env"] = [{"name": n, "value": "x"} for n in SIDECAR_ENV]
    pod["volumes"].append({"name": "booth-sidecar", "emptyDir": {"medium": "Memory"}})
    return t


def access(ws: str = "acme", role: str = "editor") -> TaskAccess:
    return TaskAccess(ws, "tok", "http://s", "http://c", role=role)


def inv(a: TaskAccess | None) -> TaskInvocation:
    return TaskInvocation("run-1", "t", "transform", 1, "pass\n", {}, {}, 60, a)


def job_for(a: TaskAccess | None) -> dict:
    cluster = FakeCluster()
    return make_runner(cluster, sidecar_template())._job("booth-pipeline-task-abc", inv(a))


def pod(job: dict) -> dict:
    return job["spec"]["template"]["spec"]


def test_a_task_with_access_gets_a_sidecar_for_its_own_workspace():
    p = pod(job_for(access("acme")))
    (sc,) = p["initContainers"]
    assert sc["args"][-3:] == ["--workspace=acme", "--scope=" + json.dumps({"workspace": "acme"}), "--access=readwrite"]
    env = {e["name"]: e["value"] for e in p["containers"][0]["env"]}
    assert env["DATABASE_URL"] == f"postgresql://localhost:5432/{workspace_database('acme')}"
    assert "@" not in env["DATABASE_URL"]  # no credential in it, ever


def test_two_tasks_from_different_workspaces_get_different_sidecars():
    a, b = pod(job_for(access("acme"))), pod(job_for(access("globex")))
    assert "--workspace=acme" in a["initContainers"][0]["args"] and "--workspace=globex" in b["initContainers"][0]["args"]
    assert "--workspace=acme" not in b["initContainers"][0]["args"]


def test_the_template_is_never_mutated_between_tasks():
    t = sidecar_template()
    before = copy.deepcopy(t)
    runner = make_runner(FakeCluster(), t)
    runner._job("j1", inv(access("acme")))
    runner._job("j2", inv(None))
    assert t == before


def test_a_viewer_task_asks_only_to_read():
    """The broker refuses readwrite to a viewer, and a refusal makes the sidecar exit."""
    (sc,) = pod(job_for(access(role="viewer")))["initContainers"]
    assert sc["args"][-1] == "--access=read"


def test_a_task_without_platform_access_gets_no_sidecar_and_no_database_url():
    """No token means no identity for the sidecar to use: it would only crash-loop."""
    p = pod(job_for(None))
    assert p.get("initContainers") == []
    assert not {e["name"] for e in p["containers"][0].get("env", [])} & {"DATABASE_URL", *SIDECAR_ENV}


def test_the_database_name_matches_booth_databases_own_derivation():
    # booth-database internal/naming.ForWorkspace (also pinned by booth-notebooks' tests).
    assert workspace_database("acme") == "bdb_ws_" + hashlib.sha256(b"booth-database/workspace/acme").hexdigest()[:24]
    assert len(workspace_database("acme")) == len("bdb_ws_") + 24


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda sc: sc.update(image="ghcr.io/projectbooth/credential-sidecar:latest"), "pinned by digest"),
        (lambda sc: sc.pop("restartPolicy"), "native sidecar"),
        (lambda sc: sc["args"].__setitem__(1, "--listen=0.0.0.0:5432"), "loopback only"),
    ],
)
def test_an_unsafe_sidecar_in_the_template_is_refused_at_startup(mutate, message):
    t = sidecar_template()
    mutate(t["spec"]["template"]["spec"]["initContainers"][0])
    with pytest.raises(ValueError, match=message):
        make_runner(FakeCluster(), t)


def test_a_template_without_a_sidecar_is_left_alone():
    p = pod(make_runner(FakeCluster(), template())._job("j", inv(access())))
    assert "initContainers" not in p
    assert "DATABASE_URL" not in {e["name"] for e in p["containers"][0].get("env", [])}


def test_the_job_runner_runs_a_sidecar_task_end_to_end(tmp_path):
    """Through the fake cluster: the personalized Job is what gets created."""
    cluster = FakeCluster()
    try:
        r: KubernetesJobRunner = make_runner(cluster, sidecar_template())
        assert r.run(inv(access("acme")), Lines(), Cancellation()) is None
        created = next(json.loads(b) for q, b in zip(cluster.requests, cluster.bodies, strict=True) if q.method == "POST" and q.url.path.endswith("/jobs"))
        assert "--workspace=acme" in pod(created)["initContainers"][0]["args"]
    finally:
        cluster.close()


# ---- the one-task runner: the token at the sidecar's --token-file, and DATABASE_URL -----------


def test_the_task_token_is_kept_where_the_sidecar_reads_it_and_removed_after(tmp_path):
    shared = str(tmp_path / "token")
    runner = SubprocessRunner(shared_token_file=shared)
    seen: list[str] = []
    started, release = threading.Event(), threading.Event()
    src = "import time\ndef run(ctx):\n    time.sleep(1.5)\n"

    def go() -> None:
        runner.run(TaskInvocation("r", "t", "transform", 1, src, {}, {}, 30, TaskAccess("acme", "tok-1", "http://s", "http://c")), Lines(), Cancellation())
        release.set()

    t = threading.Thread(target=go)
    t.start()
    for _ in range(100):
        if os.path.exists(shared):
            started.set()
            break
        threading.Event().wait(0.02)
    assert started.is_set()
    seen.append(open(shared).read())
    assert runner.update_token("r", "t", "tok-2")
    seen.append(open(shared).read())
    t.join(30)
    assert seen == ["tok-1", "tok-2"]
    assert not os.path.exists(shared)  # gone with the task


def test_a_task_without_access_writes_no_shared_token(tmp_path):
    shared = str(tmp_path / "token")
    SubprocessRunner(shared_token_file=shared).run(TaskInvocation("r", "t", "transform", 1, "pass\n", {}, {}, 30, None), Lines(), Cancellation())
    assert not os.path.exists(shared)


def test_database_url_reaches_the_task_and_nothing_else_does(monkeypatch):
    monkeypatch.setenv("SOME_MODULE_SECRET", "nope")
    lines = Lines()
    url = "postgresql://localhost:5432/bdb_ws_x"
    SubprocessRunner(task_env={"DATABASE_URL": url}).run(
        TaskInvocation("r", "t", "transform", 1, "import os\nprint(os.environ.get('DATABASE_URL'), os.environ.get('SOME_MODULE_SECRET'))\n", {}, {}, 30, None),
        lines,
        Cancellation(),
    )
    assert lines.stdout == [f"{url} None"]


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:pw@localhost:5432/db",  # a credential
        "postgresql://db.example.com:5432/db",  # not the sidecar
        "mysql://localhost:3306/db",
    ],
)
def test_only_a_credential_free_loopback_database_url_is_accepted(url):
    with pytest.raises(ValueError, match="no credentials"):
        loopback_database_url(url)


def test_a_loopback_database_url_is_accepted():
    assert loopback_database_url("postgresql://localhost:5432/bdb_ws_x") == "postgresql://localhost:5432/bdb_ws_x"


def test_a_task_waits_for_the_sidecars_lease_before_starting():
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    state = {"ready": False}
    health = FastAPI()

    @health.get("/healthz")
    def _h():
        return JSONResponse({}, status_code=200 if state["ready"] else 503)

    with serve_asgi(health) as url:
        threading.Timer(0.6, lambda: state.update(ready=True)).start()
        lines = Lines()
        t0 = time.monotonic()
        sidecar_wait(url + "/healthz", 10, poll=0.1)(inv(access()), lines)
        waited = time.monotonic() - t0
    assert 0.5 <= waited < 5 and lines.out == []


def test_a_task_starts_anyway_when_the_sidecar_never_gets_a_lease():
    lines = Lines()
    sidecar_wait("http://127.0.0.1:1/healthz", 0.3, poll=0.1)(inv(access()), lines)
    assert any(s == "system" and "no lease" in m for s, m in lines.out)


def test_a_task_without_access_does_not_wait_for_the_sidecar():
    lines = Lines()
    sidecar_wait("http://127.0.0.1:1/healthz", 30)(inv(None), lines)
    assert lines.out == []


def test_the_wait_runs_after_the_token_is_in_place_and_before_the_task_starts(tmp_path):
    """The sidecar authenticates with the task's token, so the lease wait must come after the runner
    writes it, or each waits for the other (found on a real cluster)."""
    shared = str(tmp_path / "token")
    seen: list[str] = []

    def before_start(i, log):
        seen.append(open(shared).read())  # the sidecar's --token-file already holds the task's token
        log.line("system", "waited")

    lines = Lines()
    runner = SubprocessRunner(shared_token_file=shared, before_start=before_start)
    runner.run(TaskInvocation("r", "t", "transform", 1, "print('task ran')\n", {}, {}, 30, access()), lines, Cancellation())
    assert seen == ["tok"]
    assert lines.out.index(("system", "waited")) < lines.out.index(("stdout", "task ran"))


def test_the_runner_service_runs_a_task_through_a_waiting_runner():
    app = create_runner_app("s", SubprocessRunner(before_start=sidecar_wait("http://127.0.0.1:1/healthz", 0.2, poll=0.1)))
    lines = Lines()
    with serve_asgi(app) as url:
        assert RemoteRunner(url, "s").run(inv(access()), lines, Cancellation()) is None
    assert any("no lease" in m for _, m in lines.out)
