"""KubernetesJobRunner (ADR 0096; design: docs/decisions/0018) against a fake Kubernetes API.

The fake plays the API server and the kubelet: a created Job gets a pod; once that pod's
``runner-auth`` Secret exists, the "kubelet" mounts it — starts a REAL runner service on a real
socket, authenticated with the bearer from that Secret — and reports the pod Ready with a fake pod
IP. A routing transport takes ``http://<pod IP>:8080`` to that pod's socket. So everything after
"pod Ready" is the real RemoteRunner/runner-service protocol; what is faked is only Kubernetes.

The isolation property itself (two concurrent tasks from different workspaces unable to reach each
other's Secret, pod or logs) needs a real cluster and CNI: .github/workflows/integration.yml.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from typing import Any

import httpx
import pytest

from booth_pipeline.kube import KubeClient
from booth_pipeline.runner_service import create_runner_app
from booth_pipeline.runners.base import Cancellation, TaskAccess, TaskCanceled, TaskFailed, TaskInvocation
from booth_pipeline.runners.kubejob import KEY_LABEL, RUN_LABEL, TASK_JOB_LABEL, KubernetesJobRunner
from booth_pipeline.runners.remote import RemoteRunner
from booth_pipeline.runners.subprocess_runner import SubprocessRunner

from .servers import free_port, serve_asgi

PLATFORM_TOKEN = "platform-token-that-must-never-reach-a-kubernetes-object"  # noqa: S105


def template() -> dict[str, Any]:
    """The shape the chart renders (charts/booth-pipeline/templates/task-job.yaml)."""
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"labels": {"app.kubernetes.io/component": "task"}},
        "spec": {
            "ttlSecondsAfterFinished": 60,
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/component": "task"}},
                "spec": {
                    "automountServiceAccountToken": False,
                    "containers": [{"name": "task", "image": "booth-pipeline:test", "command": ["booth-pipeline-runner"]}],
                    "volumes": [{"name": "tmp", "emptyDir": {}}, {"name": "runner-auth", "secret": {"secretName": "placeholder"}}],
                },
            },
        },
    }


class Lines:
    def __init__(self) -> None:
        self.out: list[tuple[str, str]] = []

    def line(self, stream: str, message: str) -> None:
        self.out.append((stream, message))

    @property
    def stdout(self) -> list[str]:
        return [m for s, m in self.out if s == "stdout"]


def inv(source: str, key="t", timeout=60, access: TaskAccess | None = None, run_id="run-1") -> TaskInvocation:
    return TaskInvocation(run_id, key, "transform", 1, source, {}, {}, timeout, access)


class FakeCluster:
    """The API server + kubelet, as far as one task's Job lifecycle goes."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.secrets: dict[str, dict] = {}
        self.pods: dict[str, dict] = {}  # job name -> pod
        self.ports: dict[str, int] = {}  # pod IP -> local port
        self.servers: dict[str, ExitStack] = {}
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.deleted: list[str] = []
        self.peak_outstanding = 0
        self.next_ip = 1
        self.pod_problem: dict | None = None  # e.g. a waiting reason, or "never-ready"
        self.fail_create_job: str | None = None

    # -- the API server ----------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        with self.lock:
            self.requests.append(request)
            self.bodies.append(body)
        path, method = request.url.path, request.method
        if method == "POST" and path == "/apis/batch/v1/namespaces/ns/jobs":
            if self.fail_create_job:
                return httpx.Response(403, json={"message": self.fail_create_job})
            job = json.loads(body)
            name = job["metadata"]["name"]
            job["metadata"]["uid"] = f"uid-{name}"
            with self.lock:
                self.jobs[name] = job
                self.peak_outstanding = max(self.peak_outstanding, len(self.jobs))
                self.pods[name] = {"metadata": {"name": f"{name}-pod", "labels": job["spec"]["template"]["metadata"]["labels"]}, "status": {"phase": "Pending"}}
            return httpx.Response(201, json=job)
        if method == "POST" and path == "/api/v1/namespaces/ns/secrets":
            secret = json.loads(body)
            with self.lock:
                self.secrets[secret["metadata"]["name"]] = secret
            self._kubelet_mounts(secret)
            return httpx.Response(201, json=secret)
        if method == "GET" and path == "/api/v1/namespaces/ns/pods":
            key, _, value = request.url.params["labelSelector"].partition("=")
            with self.lock:
                items = [p for p in self.pods.values() if p["metadata"]["labels"].get(key) == value]
            return httpx.Response(200, json={"items": items})
        if method == "DELETE" and path.startswith("/apis/batch/v1/namespaces/ns/jobs/"):
            name = path.rsplit("/", 1)[1]
            assert json.loads(body) == {"propagationPolicy": "Background"}
            with self.lock:
                job = self.jobs.pop(name, None)
                self.pods.pop(name, None)
                self.deleted.append(name)
                # Garbage collection: everything owner-referenced to the Job goes with it.
                uid = job and job["metadata"]["uid"]
                for n, s in list(self.secrets.items()):
                    if any(o["uid"] == uid for o in s["metadata"].get("ownerReferences", [])):
                        del self.secrets[n]
                stack = self.servers.pop(name, None)
            if stack:
                stack.close()
            return httpx.Response(200 if job else 404, json={"message": "ok" if job else "not found"})
        return httpx.Response(405, json={"message": f"the task runner's Role does not allow {method} {path}"})

    # -- the kubelet -------------------------------------------------------------------------

    def _kubelet_mounts(self, secret: dict) -> None:
        owner = secret["metadata"]["ownerReferences"][0]["name"]
        with self.lock:
            job, pod = self.jobs[owner], self.pods[owner]
        vols = {v["name"]: v for v in job["spec"]["template"]["spec"]["volumes"]}
        if vols["runner-auth"]["secret"]["secretName"] != secret["metadata"]["name"]:
            return  # the Job does not mount this Secret: its pod would never start
        if self.pod_problem == "never-ready":
            return
        if self.pod_problem == "crashed":
            pod["status"] = {"phase": "Failed", "containerStatuses": [{"name": "task", "state": {"terminated": {"exitCode": 1, "reason": "Error"}}}]}
            return
        if self.pod_problem:
            pod["status"]["containerStatuses"] = [{"name": "task", "state": {"waiting": self.pod_problem}}]
            return
        stack = ExitStack()
        url = stack.enter_context(serve_asgi(create_runner_app(secret["stringData"]["token"], SubprocessRunner(), max_concurrent=1)))
        with self.lock:
            ip = f"10.244.0.{self.next_ip}"
            self.next_ip += 1
            self.ports[ip] = int(url.rsplit(":", 1)[1])
            self.servers[owner] = stack
            pod["status"] = {"phase": "Running", "podIP": ip, "conditions": [{"type": "Ready", "status": "True"}]}

    def close(self) -> None:
        for s in list(self.servers.values()):
            s.close()


class PodNetwork(httpx.BaseTransport):
    """Routes http://<pod IP>:8080 to that fake pod's local socket."""

    def __init__(self, cluster: FakeCluster) -> None:
        self._cluster = cluster
        self._inner = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        assert request.url.port == 8080
        port = self._cluster.ports.get(request.url.host)
        if port is None:
            raise httpx.ConnectError("no route to host", request=request)
        request.url = request.url.copy_with(host="127.0.0.1", port=port)
        return self._inner.handle_request(request)

    def close(self) -> None:  # shared by every RemoteRunner; closed once by the fixture
        pass


@pytest.fixture()
def cluster():
    c = FakeCluster()
    yield c
    c.close()


def make_runner(cluster: FakeCluster, tpl: dict | None = None, **kw) -> KubernetesJobRunner:
    kube = KubeClient("https://kube.test", "ns", None, transport=httpx.MockTransport(cluster.handler))
    kw.setdefault("poll_seconds", 0.02)
    return KubernetesJobRunner(kube, tpl or template(), transport=PodNetwork(cluster), **kw)


def test_a_task_runs_in_its_own_job_and_the_job_is_deleted_afterwards(cluster):
    r = make_runner(cluster)
    lines = Lines()
    out = r.run(inv("def run(ctx):\n    print('hello from the pod')\n    return {'n': 42}\n"), lines, Cancellation())
    assert out == {"n": 42}
    assert lines.stdout == ["hello from the pod"]
    assert any(s == "system" and m.startswith("task pod ready in ") for s, m in lines.out)
    assert len(cluster.deleted) == 1 and not cluster.jobs and not cluster.secrets  # Job gone, Secret collected with it


def test_the_job_is_one_shot_hardened_and_labelled_with_its_task(cluster):
    r = make_runner(cluster, start_timeout_seconds=30)
    r.run(inv("pass\n", key="extract/orders", timeout=600, run_id="run-abc"), Lines(), Cancellation())
    job = json.loads(next(b for q, b in zip(cluster.requests, cluster.bodies, strict=True) if q.url.path.endswith("/jobs") and q.method == "POST"))
    name = job["metadata"]["name"]
    assert name.startswith("booth-pipeline-task-")
    assert job["spec"]["backoffLimit"] == 0  # a retry is the engine's call, never Kubernetes'
    assert job["spec"]["activeDeadlineSeconds"] == 600 + 30 + 120
    pod = job["spec"]["template"]
    assert pod["spec"]["restartPolicy"] == "Never" and pod["spec"]["automountServiceAccountToken"] is False
    for labels in (job["metadata"]["labels"], pod["metadata"]["labels"]):
        assert labels[TASK_JOB_LABEL] == name and labels[RUN_LABEL] == "run-abc" and labels[KEY_LABEL] == "extract_orders"
        assert labels["app.kubernetes.io/component"] == "task"  # what the NetworkPolicy selects
    auth = next(v for v in pod["spec"]["volumes"] if v["name"] == "runner-auth")
    assert auth["secret"] == {"secretName": f"{name}-auth"}  # default mode: a 0400 file would be unreadable by the non-root runner


def test_the_per_task_secret_holds_only_a_random_bearer_owned_by_the_job(cluster):
    r = make_runner(cluster)
    r.run(inv("pass\n"), Lines(), Cancellation())
    r.run(inv("pass\n"), Lines(), Cancellation())
    secrets = [json.loads(b) for q, b in zip(cluster.requests, cluster.bodies, strict=True) if q.url.path.endswith("/secrets")]
    assert len(secrets) == 2
    for s in secrets:
        assert set(s["stringData"]) == {"token"} and "data" not in s
        (owner,) = s["metadata"]["ownerReferences"]
        assert owner["kind"] == "Job" and owner["uid"] == f"uid-{owner['name']}" and s["metadata"]["name"] == f"{owner['name']}-auth"
        assert len(s["stringData"]["token"]) >= 40
    assert secrets[0]["stringData"]["token"] != secrets[1]["stringData"]["token"]  # fresh per task


def test_no_platform_token_is_ever_written_to_a_kubernetes_object(cluster):
    """The platform token travels only the API pod -> task pod connection (POST body, then PUTs)."""
    tokens = iter(f"{PLATFORM_TOKEN}-{i}" for i in range(2, 100))
    access = TaskAccess("acme", PLATFORM_TOKEN, "http://x", "http://x", refresh=lambda: next(tokens))
    src = "import os, time\ndef run(ctx):\n    time.sleep(1.2)\n    return open(os.environ['BOOTH_TOKEN_FILE']).read()\n"
    out = make_runner(cluster, refresh_seconds=0.3).run(inv(src, access=access), Lines(), Cancellation())
    assert out.startswith(PLATFORM_TOKEN + "-")  # refreshed in place, over the connection
    assert cluster.bodies and not any(PLATFORM_TOKEN.encode() in b for b in cluster.bodies)
    assert not any(PLATFORM_TOKEN in str(q.url) for q in cluster.requests)


def test_a_failing_task_still_deletes_its_job(cluster):
    lines = Lines()
    with pytest.raises(TaskFailed):
        make_runner(cluster).run(inv("raise RuntimeError('kaboom')\n"), lines, Cancellation())
    assert any("kaboom" in m for _, m in lines.out)  # the traceback streamed back before the pod went
    assert len(cluster.deleted) == 1 and not cluster.jobs and not cluster.secrets


def test_canceling_hangs_up_on_the_pod_and_deletes_its_job(cluster):
    cancel = Cancellation()
    lines = Lines()
    src = "import time\nprint('started', flush=True)\ntime.sleep(60)\n"

    def cancel_once_started() -> None:
        deadline = time.monotonic() + 15
        while "started" not in lines.stdout and time.monotonic() < deadline:
            time.sleep(0.05)
        cancel.cancel()

    t0 = time.monotonic()
    threading.Thread(target=cancel_once_started, daemon=True).start()
    with pytest.raises(TaskCanceled):
        make_runner(cluster).run(inv(src), lines, cancel)
    assert time.monotonic() - t0 < 20
    assert len(cluster.deleted) == 1 and not cluster.jobs


def test_outstanding_jobs_are_capped_at_max_concurrent(cluster):
    r = make_runner(cluster, max_concurrent=2)
    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda i=i: results.append(r.run(inv(f"import time\ndef run(ctx):\n    time.sleep(0.6)\n    return {i}\n", key=f"k{i}"), Lines(), Cancellation())))
        for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(results) == [0, 1, 2, 3, 4]
    assert cluster.peak_outstanding == 2
    assert len(cluster.deleted) == 5 and not cluster.jobs


def test_a_task_waiting_for_a_slot_can_be_canceled(cluster):
    r = make_runner(cluster, max_concurrent=1)
    blocker = threading.Thread(target=lambda: r.run(inv("import time\ntime.sleep(1.5)\n"), Lines(), Cancellation()))
    blocker.start()
    while not cluster.jobs:
        time.sleep(0.01)
    cancel = Cancellation()
    threading.Timer(0.2, cancel.cancel).start()
    with pytest.raises(TaskCanceled):
        r.run(inv("pass\n", key="queued"), Lines(), cancel)
    blocker.join(30)
    assert len(cluster.deleted) == 1  # the queued task never created a Job


def test_a_pod_that_cannot_start_fails_the_task_with_its_reason(cluster):
    cluster.pod_problem = {"reason": "ImagePullBackOff", "message": 'Back-off pulling image "booth-pipeline:nope"'}
    with pytest.raises(TaskFailed, match=r"could not start: ImagePullBackOff: Back-off pulling image"):
        make_runner(cluster).run(inv("pass\n"), Lines(), Cancellation())
    assert len(cluster.deleted) == 1 and not cluster.jobs


def test_a_runner_that_crashes_on_start_fails_the_task_with_its_exit(cluster):
    cluster.pod_problem = "crashed"
    with pytest.raises(TaskFailed, match=r"ended before the task started \(Failed: container task exited 1 \(Error\)\)"):
        make_runner(cluster).run(inv("pass\n"), Lines(), Cancellation())
    assert len(cluster.deleted) == 1


def test_a_pod_that_never_becomes_ready_times_out(cluster):
    cluster.pod_problem = "never-ready"
    r = make_runner(cluster, start_timeout_seconds=1)
    with pytest.raises(TaskFailed, match="did not become ready within 1s"):
        r.run(inv("pass\n"), Lines(), Cancellation())
    assert len(cluster.deleted) == 1


def test_a_refused_job_is_a_task_failure_with_the_api_servers_reason(cluster):
    cluster.fail_create_job = 'jobs.batch is forbidden: exceeded quota "booth"'
    with pytest.raises(TaskFailed, match="could not start the task's pod: Kubernetes API 403: .*exceeded quota"):
        make_runner(cluster).run(inv("pass\n"), Lines(), Cancellation())


def test_the_runner_only_uses_what_its_role_grants(cluster):
    """jobs create/delete, secrets create, pods list — never secrets get, never pods/log."""
    make_runner(cluster).run(inv("print('x')\n"), Lines(), Cancellation())
    seen = {(q.method, q.url.path.rsplit("/", 1)[0] if q.method == "DELETE" else q.url.path) for q in cluster.requests}
    assert seen == {
        ("POST", "/apis/batch/v1/namespaces/ns/jobs"),
        ("POST", "/api/v1/namespaces/ns/secrets"),
        ("GET", "/api/v1/namespaces/ns/pods"),
        ("DELETE", "/apis/batch/v1/namespaces/ns/jobs"),
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda t: t["spec"]["template"]["spec"].pop("automountServiceAccountToken"), "automountServiceAccountToken: false"),
        (lambda t: t["spec"]["template"]["spec"]["volumes"].pop(), "runner-auth"),
        (lambda t: t["spec"]["template"]["spec"].update(containers=[]), "no container"),
        (lambda t: t["spec"].pop("template"), "malformed"),
    ],
)
def test_an_unusable_template_is_refused_at_startup(cluster, mutate, message):
    t = template()
    mutate(t)
    with pytest.raises(ValueError, match=message):
        make_runner(cluster, t)


# ---- the runner service's one-shot mode, as the task pod runs it --------------------------------


def _start_runner(tmp_path, **env) -> tuple[subprocess.Popen, str]:
    (tmp_path / "token").write_text("one-shot-bearer\n")
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "booth_pipeline.runner_service"],
        env={**os.environ, "BOOTH_RUNNER_AUTH_TOKEN_FILE": str(tmp_path / "token"), "BOOTH_RUNNER_PORT": str(port), "BOOTH_RUNNER_MAX_CONCURRENT": "1", **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while True:
        try:
            if httpx.get(url + "/healthz").status_code == 200:
                return proc, url
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline or proc.poll() is not None:
            proc.kill()
            raise RuntimeError("runner service did not start")
        time.sleep(0.1)


def test_a_one_shot_runner_exits_after_its_task(tmp_path):
    proc, url = _start_runner(tmp_path, BOOTH_RUNNER_ONE_SHOT="1")
    try:
        lines = Lines()
        assert RemoteRunner(url, "one-shot-bearer").run(inv("def run(ctx):\n    return 'done'\n"), lines, Cancellation()) == "done"
        assert proc.wait(timeout=20) == 0  # the Job completes on its own if nobody deletes it
    finally:
        proc.kill()


def test_a_one_shot_runner_nobody_calls_exits_when_idle(tmp_path):
    proc, _ = _start_runner(tmp_path, BOOTH_RUNNER_ONE_SHOT="1", BOOTH_RUNNER_IDLE_EXIT_SECONDS="1")
    try:
        assert proc.wait(timeout=20) == 0
    finally:
        proc.kill()


def test_an_unauthenticated_call_does_not_use_up_a_one_shot_runner(tmp_path):
    proc, url = _start_runner(tmp_path, BOOTH_RUNNER_ONE_SHOT="1")
    try:
        assert httpx.post(url + "/v1/run", json={}, headers={"authorization": "Bearer wrong"}).status_code == 401
        time.sleep(0.5)
        assert proc.poll() is None
        assert RemoteRunner(url, "one-shot-bearer").run(inv("def run(ctx):\n    return 1\n"), Lines(), Cancellation()) == 1
        assert proc.wait(timeout=20) == 0
    finally:
        proc.kill()
