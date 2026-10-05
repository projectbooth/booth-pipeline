"""The runner service: what executes a workspace member's code (ADR 0057), one task per pod.

Since ADR 0096 it runs inside each task's own Kubernetes Job pod, for exactly that one task
(``BOOTH_RUNNER_MAX_CONCURRENT=1``, ``BOOTH_RUNNER_ONE_SHOT=1``: it exits once the task's stream
ends, or if no task arrives within ``BOOTH_RUNNER_IDLE_EXIT_SECONDS``). The pod holds **no module
credentials at all** — no database DSN, no booth-workload-minting-credentials Secret, no
service-account token, no RBAC — and a NetworkPolicy lets only the API/scheduler pod call it. The
only privileged thing it ever holds is its own task's short-lived, role-ceilinged platform token
(ADR 0056), pushed by the API/scheduler pod, which is the only pod that can mint. Being one task per
pod is what keeps one task from reading another's token (they no longer share a filesystem, /tmp
or /proc).

Protocol (internal; authenticated with a per-task bearer secret, ADR 0096):

* ``POST /v1/run`` — body is one task invocation; the response is NDJSON, streamed while the task
  runs: ``{"t":"log",...}`` lines, then exactly one ``{"t":"result",...}`` or ``{"t":"error",...}``.
  ``{"t":"ping"}`` is keep-alive. **Closing the connection cancels the task** (the process tree is killed).
* ``PUT /v1/run/{run_id}/{task_key}/token`` — replace that running task's platform token.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import queue
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from .runners.base import Cancellation, TaskAccess, TaskCanceled, TaskFailed, TaskInvocation, TaskLog
from .runners.subprocess_runner import SubprocessRunner

log = logging.getLogger(__name__)

PING_SECONDS = 15
MAX_BODY_BYTES = 64 * 1024 * 1024


class AccessBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace: str
    token: str
    storageUrl: str  # noqa: N815 - wire names
    catalogUrl: str  # noqa: N815


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runId: str  # noqa: N815
    taskKey: str  # noqa: N815
    kind: str
    attempt: int
    source: str
    params: dict[str, Any]
    inputs: dict[str, Any]
    timeoutSeconds: int  # noqa: N815
    access: AccessBody | None = None


class TokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str


class _QueueLog:
    def __init__(self, q: queue.Queue[dict[str, Any]]) -> None:
        self._q = q

    def line(self, stream: str, message: str) -> None:
        self._q.put({"t": "log", "stream": stream, "message": message})


def create_runner_app(
    secret: str,
    runner: SubprocessRunner | None = None,
    max_concurrent: int = 16,
    on_task_done: Callable[[], None] | None = None,
    on_task_start: Callable[[], None] | None = None,
) -> FastAPI:
    """``on_task_start`` is called when an authenticated task is accepted; ``on_task_done`` once its
    stream has ended for any reason (result, failure, or the caller hanging up). A one-task Job pod
    (ADR 0096) uses them to shut itself down after its task, or if no task ever arrives."""
    if not secret:
        # Never construct an open runner: it executes arbitrary code for whoever can reach it.
        raise ValueError("the runner requires a non-empty shared secret")
    runner = runner or SubprocessRunner()
    slots = threading.BoundedSemaphore(max_concurrent)
    app = FastAPI(title="booth-pipeline-runner", docs_url=None, redoc_url=None, openapi_url=None)
    want = f"Bearer {secret}".encode()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Authenticate FIRST, before the body is read or validated: an unauthenticated caller gets
        # nothing — not a schema error, not a parse of a large payload. Constant-time comparison:
        # this secret is all that stands between the network and code execution.
        if request.url.path.startswith("/v1/"):
            got = request.headers.get("authorization", "").encode()
            if not hmac.compare_digest(got, want):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"error": "request body too large"}, status_code=413)
        return await call_next(request)

    @app.exception_handler(HTTPException)
    async def _http(_: Request, e: HTTPException):
        return JSONResponse({"error": str(e.detail)}, status_code=e.status_code)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "module": "pipeline-runner"}

    @app.put("/v1/run/{run_id}/{task_key}/token")
    def put_token(run_id: str, task_key: str, body: TokenBody):
        if not runner.update_token(run_id, task_key, body.token):
            raise HTTPException(404, "that task is not running here")
        return {"ok": True}

    @app.post("/v1/run")
    async def run(body: RunBody, request: Request):
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "the runner is at capacity")
        if on_task_start is not None:
            on_task_start()
        access = (
            TaskAccess(body.access.workspace, body.access.token, body.access.storageUrl, body.access.catalogUrl) if body.access else None
        )
        inv = TaskInvocation(
            run_id=body.runId,
            task_key=body.taskKey,
            kind=body.kind,
            attempt=body.attempt,
            source=body.source,
            params=body.params,
            inputs=body.inputs,
            timeout_seconds=body.timeoutSeconds,
            access=access,
        )
        events: queue.Queue[dict[str, Any]] = queue.Queue()
        cancel = Cancellation()

        def work() -> None:
            try:
                output = runner.run(inv, _QueueLog(events), cancel)
                events.put({"t": "result", "output": output})
            except TaskCanceled:
                events.put({"t": "error", "kind": "canceled", "message": "canceled"})
            except TaskFailed as e:
                events.put({"t": "error", "kind": "failed", "message": str(e)})
            except Exception as e:  # noqa: BLE001 - a runner bug is a task failure, not a dead stream
                log.exception("runner error")
                events.put({"t": "error", "kind": "failed", "message": f"runner error: {e.__class__.__name__}"})
            finally:
                events.put({"t": "end"})
                slots.release()

        threading.Thread(target=work, name=f"task-{body.runId[:8]}-{body.taskKey}", daemon=True).start()

        async def stream():
            finished = False
            idle = 0.0
            try:
                while True:
                    try:
                        ev = await asyncio.to_thread(events.get, True, 0.2)
                    except queue.Empty:
                        idle += 0.2
                        if idle >= PING_SECONDS:
                            idle = 0.0
                            yield b'{"t":"ping"}\n'
                        if await request.is_disconnected():
                            return  # the API pod hung up: that IS the cancel signal (see finally)
                        continue
                    idle = 0.0
                    if ev["t"] == "end":
                        finished = True
                        return
                    yield (json.dumps(ev) + "\n").encode("utf-8")
            finally:
                # However this generator ends — disconnect, cancellation, GeneratorExit, an error —
                # if the task has not finished, stop it: nobody is listening for its result any more,
                # and an orphaned task must not keep running (or holding a platform token).
                if not finished:
                    cancel.cancel()
                if on_task_done is not None:
                    on_task_done()

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    return app


def sidecar_wait(healthz: str, seconds: float, poll: float = 0.5) -> Callable[[TaskInvocation, TaskLog], None]:
    """Before a task with platform access starts, wait (bounded) for each of the pod's credential
    sidecars to hold a lease. ``healthz`` is a comma-separated list of their /healthz URLs, all
    loopback-only, so only reachable from inside this pod. The contract: a task that starts before its
    sidecar has a lease "should wait, not fail". A task that never touches the database or s3 must
    still run, so after ``seconds`` it starts anyway, with a note naming what is still missing."""
    urls = [u.strip() for u in healthz.split(",") if u.strip()]

    def ready(url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310 - fixed loopback URLs from the Job
                return r.status == 200
        except OSError:
            return False

    def wait(inv: TaskInvocation, log_: TaskLog) -> None:
        if inv.access is None or not urls:
            return
        deadline = time.monotonic() + seconds
        pending = list(urls)
        while True:
            pending = [u for u in pending if not ready(u)]
            if not pending:
                return
            if time.monotonic() >= deadline:
                which = ", ".join(_sidecar_name(u) for u in pending)
                log_.line("system", f"the {which} credential sidecar has no lease after {seconds:.0f}s; starting the task anyway (it will fail to connect until it does)")
                return
            time.sleep(poll)

    return wait


def _sidecar_name(healthz_url: str) -> str:
    return "database" if urllib.parse.urlsplit(healthz_url).port == 5432 else "s3"


SIDECAR_FILES_DIR = "/var/run/booth-sidecar-s3/"


def sidecar_file_path(name: str, path: str) -> str:
    """AWS_SHARED_CREDENTIALS_FILE / AWS_CONFIG_FILE may reach a task only as paths on the s3
    sidecar's private volume: a path, never a credential, and never somewhere else on the pod."""
    if not path.startswith(SIDECAR_FILES_DIR) or ".." in path.split("/"):
        raise ValueError(f"{name} must be a file under {SIDECAR_FILES_DIR} (ADR 0095)")
    return path


def loopback_database_url(url: str) -> str:
    """DATABASE_URL may reach a task only as the credential-free loopback address of its sidecar."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("postgresql", "postgres") or parsed.hostname not in ("localhost", "127.0.0.1") or parsed.password or parsed.username:
        raise ValueError("DATABASE_URL must be postgresql://localhost:<port>/<db> with no credentials (ADR 0095)")
    return url


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    path = os.environ.get("BOOTH_RUNNER_AUTH_TOKEN_FILE", "")
    secret = os.environ.get("BOOTH_RUNNER_AUTH_TOKEN", "")
    if path:
        with open(path, encoding="utf-8") as f:
            secret = f.read().strip()
    if not secret:
        print("booth-pipeline-runner: BOOTH_RUNNER_AUTH_TOKEN_FILE (or BOOTH_RUNNER_AUTH_TOKEN) is required", file=sys.stderr)
        return 2
    python = os.environ.get("BOOTH_PIPELINE_RUNNER_PYTHON") or None
    passthrough = tuple(x.strip() for x in os.environ.get("BOOTH_PIPELINE_RUNNER_ENV_PASSTHROUGH", "").split(",") if x.strip())
    one_shot = os.environ.get("BOOTH_RUNNER_ONE_SHOT", "").lower() in ("1", "true", "yes")
    server: uvicorn.Server | None = None
    started = threading.Event()

    def task_done() -> None:
        # One task per pod (ADR 0096): once its stream ends, exit so the Job completes and is cleaned
        # up even if the API/scheduler pod that should delete it is gone.
        if server is not None:
            server.should_exit = True

    # ADR 0095: the pod's credential sidecar, when the chart adds one (one-task Job pods only).
    task_env = {}
    if os.environ.get("DATABASE_URL"):
        task_env["DATABASE_URL"] = loopback_database_url(os.environ["DATABASE_URL"])
    for name in ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE"):
        if os.environ.get(name):
            task_env[name] = sidecar_file_path(name, os.environ[name])
    healthz = os.environ.get("BOOTH_RUNNER_SIDECAR_HEALTHZ", "")
    app = create_runner_app(
        secret,
        SubprocessRunner(
            python=python,
            env_passthrough=passthrough,
            shared_token_file=os.environ.get("BOOTH_RUNNER_SIDECAR_TOKEN_FILE") or None,
            task_env=task_env,
            # After the token is written (the sidecar authenticates with it), before the task starts.
            before_start=sidecar_wait(healthz, float(os.environ.get("BOOTH_RUNNER_SIDECAR_WAIT_SECONDS", "60"))) if healthz else None,
        ),
        int(os.environ.get("BOOTH_RUNNER_MAX_CONCURRENT", "16")),
        on_task_done=task_done if one_shot else None,
        on_task_start=started.set if one_shot else None,
    )
    if one_shot:
        # Nobody ever connected (the API/scheduler pod died between creating this Job and calling
        # it): don't sit idle until the Job's deadline.
        idle = int(os.environ.get("BOOTH_RUNNER_IDLE_EXIT_SECONDS", "300"))
        timer = threading.Timer(idle, lambda: started.is_set() or task_done())
        timer.daemon = True  # never keeps the process alive after a normal exit
        timer.start()
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("BOOTH_RUNNER_PORT", "8080")), log_level="info"))  # noqa: S104
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
