"""One Kubernetes Job — one pod — per task attempt (ADR 0096, ratified design: docs/decisions/0018).

Why: in the old shared runner pod every task ran as the same Unix user, so any task could read any
concurrently-running task's workdir and ``/proc/<pid>/environ`` — and with them its platform token,
across workspaces. Giving each task its own pod gives it its own filesystem, ``/tmp`` and process
namespace, so there is nothing of another task's to reach.

How, per attempt (everything after step 4 is the existing, tested ``RemoteRunner`` protocol):

1. Wait for a slot: ``max_concurrent`` caps outstanding Jobs (``runner.maxConcurrent``).
2. Create the Job from the chart-rendered template: the existing ``booth-pipeline-runner``
   entrypoint for exactly one task, the unchanged hardening, no ServiceAccount token.
3. Create a Secret holding a fresh random runner bearer — never a platform token — owner-referenced
   to the Job, so it is garbage-collected with it.
4. Wait for the pod to be Ready (its readiness probe is the runner service's ``/healthz``).
5. ``RemoteRunner(http://<pod IP>, <bearer>)``: the invocation goes in the POST body (no 1 MiB
   Kubernetes object limit), logs and the result stream back, token refreshes are instant PUTs.
6. Always delete the Job — success, failure, timeout, cancellation. Cancellation also hangs up the
   stream first, which kills the task at once.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import secrets
import threading
import time
from typing import Any
from uuid import uuid4

import httpx

from ..kube import KubeClient, KubeError
from .base import Cancellation, TaskCanceled, TaskFailed, TaskInvocation, TaskLog
from .remote import DEFAULT_REFRESH_SECONDS, RemoteRunner

log = logging.getLogger(__name__)

TASK_JOB_LABEL = "booth.projectbooth.io/task-job"
RUN_LABEL = "booth.projectbooth.io/run-id"
KEY_LABEL = "booth.projectbooth.io/task-key"
AUTH_VOLUME = "runner-auth"  # the template's volume whose Secret this runner fills in per task
# The chart's credential sidecars (ADR 0095), and the task-container variables that exist only for
# them. KubernetesJobRunner personalizes each per task, or removes it from a task it can't serve.
POSTGRES_SIDECAR = "credential-sidecar-postgres"
S3_SIDECAR = "credential-sidecar-s3"
_POSTGRES_ENV = {"DATABASE_URL"}
_S3_ENV = {"AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE"}
_COMMON_SIDECAR_ENV = {"BOOTH_RUNNER_SIDECAR_TOKEN_FILE", "BOOTH_RUNNER_SIDECAR_HEALTHZ", "BOOTH_RUNNER_SIDECAR_WAIT_SECONDS"}
WAREHOUSE_TIMEOUT_SECONDS = 10.0

# A pod that is waiting for one of these will not start on its own: fail the task with the reason
# rather than sit out the whole start timeout.
_FATAL_WAITING = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CreateContainerConfigError", "CreateContainerError", "CrashLoopBackOff"}


class KubernetesJobRunner:
    id = "base"  # still the base runner (a task's `runner` is "base"), just one pod per task

    def __init__(
        self,
        kube: KubeClient,
        job_template: dict[str, Any],
        *,
        max_concurrent: int = 16,
        start_timeout_seconds: int = 300,
        name_prefix: str = "booth-pipeline-task",
        task_port: int = 8080,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        poll_seconds: float = 0.5,
        transport: httpx.BaseTransport | None = None,
        lakehouse_transport: httpx.BaseTransport | None = None,
    ) -> None:
        _check_template(job_template, task_port)
        self._kube = kube
        self._template = job_template
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._start_timeout = start_timeout_seconds
        self._prefix = name_prefix[:40].rstrip("-")
        self._port = task_port
        self._refresh = refresh_seconds
        self._poll = poll_seconds
        self._transport = transport  # tests only: how RemoteRunner reaches a "pod"
        self._lakehouse_transport = lakehouse_transport  # tests only: how the warehouse lookup reaches core

    @classmethod
    def from_template_file(cls, path: str, **kw: Any) -> KubernetesJobRunner:
        with open(path, encoding="utf-8") as f:
            return cls(KubeClient.in_cluster(), json.load(f), **kw)

    def run(self, inv: TaskInvocation, log_: TaskLog, cancel: Cancellation) -> Any:
        if cancel.canceled:
            raise TaskCanceled()
        self._acquire(cancel)
        try:
            return self._run_in_job(inv, log_, cancel)
        finally:
            self._slots.release()

    # ---- internals ---------------------------------------------------------------------------

    def _acquire(self, cancel: Cancellation) -> None:
        while not self._slots.acquire(timeout=self._poll):
            if cancel.canceled:
                raise TaskCanceled()

    def _run_in_job(self, inv: TaskInvocation, log_: TaskLog, cancel: Cancellation) -> Any:
        name = f"{self._prefix}-{uuid4().hex[:12]}"
        bearer = secrets.token_urlsafe(32)
        t0 = time.monotonic()
        warehouse = self._warehouse(inv, log_)
        try:
            job = self._kube.create_job(self._job(name, inv, warehouse))
        except KubeError as e:
            raise TaskFailed(f"could not start the task's pod: {e}") from None
        try:
            try:
                self._kube.create_secret(_secret(name, bearer, job))
            except KubeError as e:
                raise TaskFailed(f"could not start the task's pod: {e}") from None
            ip = self._wait_ready(name, cancel)
            log_.line("system", f"task pod ready in {time.monotonic() - t0:.1f}s")
            host = f"[{ip}]" if ":" in ip else ip
            remote = RemoteRunner(f"http://{host}:{self._port}", bearer, refresh_seconds=self._refresh, transport=self._transport)
            try:
                return remote.run(inv, log_, cancel)
            finally:
                remote.close()
        finally:
            try:
                self._kube.delete_job(name)
            except Exception:  # noqa: BLE001 - never mask the task's own outcome; the Job's deadline is the backstop
                log.exception("could not delete task Job %s", name)

    def _warehouse(self, inv: TaskInvocation, log_: TaskLog) -> dict[str, str] | None:
        """The s3 sidecar's scope (ADR 0095, third amendment): this task's workspace's lakehouse
        warehouse, `{backendId, path}`, from booth-lakehouse's GET /api/warehouse through core's
        gateway, asked AS THE TASK (its own token). None, and so no s3 sidecar, when the chart has
        none, the task has no platform access, or the workspace has no warehouse (404). This makes
        booth-lakehouse a runtime dependency at task start, for s3-enabled installs only."""
        sidecar = _init_container(self._template["spec"]["template"]["spec"], S3_SIDECAR)
        if sidecar is None or inv.access is None:
            return None
        core = _arg(sidecar, "--core-url").rstrip("/")
        try:
            with httpx.Client(transport=self._lakehouse_transport, timeout=WAREHOUSE_TIMEOUT_SECONDS) as http:
                r = http.get(
                    f"{core}/modules/lakehouse/api/warehouse",
                    headers={"Authorization": f"Bearer {inv.access.token}", "X-Workspace": inv.access.workspace},
                )
            if r.status_code == 404:
                return None  # no warehouse yet: no s3 sidecar, exactly like a task without access
            r.raise_for_status()
            body = r.json()
            return {"backendId": str(body["backendId"]), "path": str(body["path"])}
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            # booth-lakehouse being down must not fail a task that may never touch s3; say so instead.
            reason = f"HTTP {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError) else e.__class__.__name__
            log_.line("system", f"could not look up this workspace's lakehouse warehouse ({reason}); the task runs without s3 credentials")
            return None

    def _job(self, name: str, inv: TaskInvocation, warehouse: dict[str, str] | None = None) -> dict[str, Any]:
        job = copy.deepcopy(self._template)
        labels = {TASK_JOB_LABEL: name, RUN_LABEL: _label_value(inv.run_id), KEY_LABEL: _label_value(inv.task_key)}
        meta = job.setdefault("metadata", {})
        meta["name"] = name
        meta.setdefault("labels", {}).update(labels)
        spec = job.setdefault("spec", {})
        spec["backoffLimit"] = 0  # a retry is the engine's decision (retry policy), never Kubernetes'
        # The backstop if this pod's API/scheduler pod dies mid-task and never deletes it.
        spec["activeDeadlineSeconds"] = inv.timeout_seconds + self._start_timeout + 120
        tpl = spec.setdefault("template", {})
        tpl.setdefault("metadata", {}).setdefault("labels", {}).update(labels)
        pod = tpl["spec"]
        pod["restartPolicy"] = "Never"
        _personalize_sidecars(pod, inv, warehouse)
        for v in pod["volumes"]:
            if v["name"] == AUTH_VOLUME:
                # Default file mode on purpose: a 0400 file is root-owned and unreadable by the
                # non-root runner (no fsGroup). Within a one-task pod a tighter mode protects nothing
                # anyway — the task's code runs as the same user as the runner reading it.
                v["secret"] = {"secretName": _secret_name(name)}
        return job

    def _wait_ready(self, name: str, cancel: Cancellation) -> str:
        deadline = time.monotonic() + self._start_timeout
        while True:
            if cancel.canceled:
                raise TaskCanceled()
            for pod in self._kube.list_pods(f"{TASK_JOB_LABEL}={name}"):
                status = pod.get("status", {})
                if status.get("phase") in ("Failed", "Succeeded"):
                    raise TaskFailed(f"the task's pod ended before the task started ({status.get('phase')}: {_why_ended(status)})")
                for cs in status.get("containerStatuses", []):
                    waiting = cs.get("state", {}).get("waiting") or {}
                    if waiting.get("reason") in _FATAL_WAITING:
                        raise TaskFailed(f"the task's pod could not start: {waiting.get('reason')}: {waiting.get('message', '')}".rstrip(": "))
                ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []))
                if ready and status.get("podIP"):
                    return status["podIP"]
            if time.monotonic() >= deadline:
                raise TaskFailed(f"the task's pod did not become ready within {self._start_timeout}s")
            time.sleep(self._poll)


def workspace_database(workspace: str) -> str:
    """The workspace's database name in booth-database (its internal/naming.ForWorkspace; the same
    derivation booth-notebooks uses). Informational: the sidecar connects to whatever database its
    credential names, so this only makes DATABASE_URL agree with current_database()."""
    return "bdb_ws_" + hashlib.sha256(f"booth-database/workspace/{workspace}".encode()).hexdigest()[:24]


def _init_container(pod: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((c for c in pod.get("initContainers", []) if c.get("name") == name), None)


def _arg(container: dict[str, Any], flag: str) -> str:
    return next((a.split("=", 1)[1] for a in container.get("args", []) if a.startswith(flag + "=")), "")


def _personalize_sidecars(pod: dict[str, Any], inv: TaskInvocation, warehouse: dict[str, str] | None) -> None:
    """Fill in the chart-rendered credential sidecars (ADR 0095) for THIS task's identity, or remove them.

    A sidecar authenticates as the task (its --token-file is the task's own platform token), so a task
    with no platform access gets none. The s3 sidecar also needs the workspace's warehouse as its
    scope; with none (no warehouse yet), that task gets no s3 sidecar either. Whatever remains decides
    which lease(s) the runner waits for before starting the task.
    """
    task = pod["containers"][0]
    env_drop: set[str] = set()
    keep: list[dict[str, Any]] = []
    healthz: list[str] = []
    # A viewer's token may only read: the broker refuses `readwrite` to it, and a refusal makes the
    # sidecar exit (contracts/credential-sidecar.md).
    access = "read" if inv.access is not None and inv.access.role == "viewer" else "readwrite"
    for c in pod.get("initContainers", []):
        name = c.get("name")
        if name == POSTGRES_SIDECAR:
            if inv.access is None:
                env_drop |= _POSTGRES_ENV
                continue
            ws = inv.access.workspace
            c["args"] = [*c.get("args", []), f"--workspace={ws}", "--scope=" + json.dumps({"workspace": ws}), f"--access={access}"]
            task.setdefault("env", []).append({"name": "DATABASE_URL", "value": f"postgresql://localhost:5432/{workspace_database(ws)}"})
            healthz.append(f"http://{_arg(c, '--listen')}/healthz")
        elif name == S3_SIDECAR:
            if inv.access is None or warehouse is None:
                env_drop |= _S3_ENV
                continue
            scope = {"backendId": warehouse["backendId"], "path": warehouse["path"]}
            c["args"] = [*c.get("args", []), f"--workspace={inv.access.workspace}", "--scope=" + json.dumps(scope), f"--access={access}"]
            healthz.append(f"http://{_arg(c, '--health-listen')}/healthz")
        keep.append(c)
    if "initContainers" in pod:
        pod["initContainers"] = keep
    if not healthz:
        env_drop |= _COMMON_SIDECAR_ENV
    env = [e for e in task.get("env", []) if e["name"] not in env_drop and e["name"] != "BOOTH_RUNNER_SIDECAR_HEALTHZ"]
    if healthz:
        env.append({"name": "BOOTH_RUNNER_SIDECAR_HEALTHZ", "value": ",".join(healthz)})
    if "env" in task or env:
        task["env"] = env


def _why_ended(status: dict[str, Any]) -> str:
    """The pod's own reason, else its container's exit (the runner's logs say more; we may not read them)."""
    if status.get("reason") or status.get("message"):
        return str(status.get("reason") or status.get("message"))
    for cs in status.get("containerStatuses", []):
        term = cs.get("state", {}).get("terminated")
        if term:
            return f"container {cs.get('name')} exited {term.get('exitCode')} ({term.get('reason') or 'no reason'})"
    return "no reason given"


def _secret_name(job_name: str) -> str:
    return f"{job_name}-auth"


def _secret(job_name: str, bearer: str, job: dict[str, Any]) -> dict[str, Any]:
    """The per-task Secret: ONLY the bearer authenticating the API pod to this one task's runner
    service. Never a platform token. Owner-referenced to the Job, so it goes when the Job does."""
    meta = job["metadata"]
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": _secret_name(job_name),
            "labels": {TASK_JOB_LABEL: job_name},
            "ownerReferences": [{"apiVersion": "batch/v1", "kind": "Job", "name": meta["name"], "uid": meta["uid"]}],
        },
        "type": "Opaque",
        "stringData": {"token": bearer},
    }


def _label_value(v: str) -> str:
    """Kubernetes label values: at most 63 of [A-Za-z0-9_.-], alphanumeric at both ends."""
    cleaned = "".join(c if c.isalnum() or c in "_.-" else "_" for c in v)[:63]
    return cleaned.strip("_.-") or "x"


def _check_template(t: dict[str, Any], task_port: int = 8080) -> None:
    """Fail at startup, not on the first run, if the chart-rendered template can't be used."""
    try:
        pod = t["spec"]["template"]["spec"]
        names = {v["name"] for v in pod["volumes"]}
        containers = pod["containers"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"task Job template is malformed: missing {e}") from None
    if AUTH_VOLUME not in names:
        raise ValueError(f"task Job template must declare a volume named {AUTH_VOLUME!r} for the per-task runner bearer")
    if pod.get("automountServiceAccountToken") is not False:
        raise ValueError("task Job template must set automountServiceAccountToken: false (ADR 0057/0096)")
    if not containers:
        raise ValueError("task Job template has no container")
    for c in pod.get("initContainers", []):
        name = c.get("name")
        if name not in (POSTGRES_SIDECAR, S3_SIDECAR):
            continue
        if not re.fullmatch(r"\S+@sha256:[0-9a-f]{64}", c.get("image", "")):
            raise ValueError("the credential sidecar image must be pinned by digest (…/credential-sidecar@sha256:<64 hex>), never a tag (ADR 0095)")
        if c.get("restartPolicy") != "Always":
            raise ValueError("the credential sidecar must be a native sidecar (restartPolicy: Always), or it keeps the task's pod alive after the task")
        listen = _arg(c, "--listen" if name == POSTGRES_SIDECAR else "--health-listen")
        if not listen.startswith(("127.0.0.1:", "localhost:")):
            raise ValueError(f"the credential sidecar must listen on loopback only, not {listen or 'its default'!r} (contracts/credential-sidecar.md)")
        if listen.rsplit(":", 1)[1] == str(task_port):
            # The runner binds 0.0.0.0:<task_port> in the same pod; the s3 sidecar's own default is :8080.
            raise ValueError(f"the credential sidecar's {listen} collides with the runner's port {task_port}")
        if name == S3_SIDECAR and not _arg(c, "--core-url"):
            raise ValueError("the s3 credential sidecar needs --core-url (its warehouse lookup goes through core's gateway)")
