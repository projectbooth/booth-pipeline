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
# The chart's postgres credential sidecar (ADR 0095), and the task-container variables that exist only
# for it; KubernetesJobRunner personalizes it per task, or removes it from a task with no identity.
POSTGRES_SIDECAR = "credential-sidecar-postgres"
_SIDECAR_TASK_ENV = {"DATABASE_URL", "BOOTH_RUNNER_SIDECAR_TOKEN_FILE", "BOOTH_RUNNER_SIDECAR_HEALTHZ", "BOOTH_RUNNER_SIDECAR_WAIT_SECONDS"}

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
    ) -> None:
        _check_template(job_template)
        self._kube = kube
        self._template = job_template
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._start_timeout = start_timeout_seconds
        self._prefix = name_prefix[:40].rstrip("-")
        self._port = task_port
        self._refresh = refresh_seconds
        self._poll = poll_seconds
        self._transport = transport  # tests only: how RemoteRunner reaches a "pod"

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
        try:
            job = self._kube.create_job(self._job(name, inv))
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

    def _job(self, name: str, inv: TaskInvocation) -> dict[str, Any]:
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
        _personalize_sidecars(pod, inv)
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


def _personalize_sidecars(pod: dict[str, Any], inv: TaskInvocation) -> None:
    """Fill in the chart-rendered credential sidecar (ADR 0095) for THIS task's identity, or remove it.

    The sidecar authenticates as the task (its --token-file is the task's own platform token), so a
    task with no platform access has no identity to give it: it gets no sidecar and no DATABASE_URL.
    """
    inits = pod.get("initContainers", [])
    sidecar = next((c for c in inits if c.get("name") == POSTGRES_SIDECAR), None)
    if sidecar is None:
        return
    task = pod["containers"][0]
    if inv.access is None:
        pod["initContainers"] = [c for c in inits if c is not sidecar]
        task["env"] = [e for e in task.get("env", []) if e["name"] not in _SIDECAR_TASK_ENV]
        return
    ws = inv.access.workspace
    # A viewer's token may only read: the broker refuses `readwrite` to it, and a refusal makes the
    # sidecar exit (contracts/credential-sidecar.md).
    access = "read" if inv.access.role == "viewer" else "readwrite"
    sidecar["args"] = [*sidecar.get("args", []), f"--workspace={ws}", "--scope=" + json.dumps({"workspace": ws}), f"--access={access}"]
    task.setdefault("env", []).append({"name": "DATABASE_URL", "value": f"postgresql://localhost:5432/{workspace_database(ws)}"})


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


def _check_template(t: dict[str, Any]) -> None:
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
        if c.get("name") != POSTGRES_SIDECAR:
            continue
        if not re.fullmatch(r"\S+@sha256:[0-9a-f]{64}", c.get("image", "")):
            raise ValueError("the credential sidecar image must be pinned by digest (…/credential-sidecar@sha256:<64 hex>), never a tag (ADR 0095)")
        if c.get("restartPolicy") != "Always":
            raise ValueError("the credential sidecar must be a native sidecar (restartPolicy: Always), or it keeps the task's pod alive after the task")
        listen = next((a.split("=", 1)[1] for a in c.get("args", []) if a.startswith("--listen=")), "")
        if not listen.startswith(("127.0.0.1:", "localhost:")):
            raise ValueError(f"the credential sidecar must listen on loopback only, not {listen or 'its default'!r} (contracts/credential-sidecar.md)")
