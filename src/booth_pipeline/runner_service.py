"""The runner service: the pod that executes workspace members' code (ADR 0057).

It runs in its own Deployment with **no module credentials at all** — no database DSN, no
booth-workload-minting-credentials Secret, no service-account token, no RBAC — and a NetworkPolicy
that only lets the API/scheduler pod call it. That absence *is* the security boundary: task code
here can read everything in this pod and none of it is a standing credential. The only privileged
thing it ever holds is one task's short-lived, role-ceilinged platform token (ADR 0056), pushed to
it by the API/scheduler pod, which is the only pod that can mint.

Protocol (internal; authenticated with a shared bearer secret):

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
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from .runners.base import Cancellation, TaskAccess, TaskCanceled, TaskFailed, TaskInvocation
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


def create_runner_app(secret: str, runner: SubprocessRunner | None = None, max_concurrent: int = 16) -> FastAPI:
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

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    return app


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
    app = create_runner_app(secret, SubprocessRunner(python=python, env_passthrough=passthrough), int(os.environ.get("BOOTH_RUNNER_MAX_CONCURRENT", "16")))
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("BOOTH_RUNNER_PORT", "8080")), log_level="info")  # noqa: S104
    return 0


if __name__ == "__main__":
    sys.exit(main())
