"""A stand-in for booth-spark's ``/v1`` applications API behind core's gateway (``/modules/spark``), for
booth-pipeline's Spark runner tests (ADR 0119). Test fixture only. Shared by the unit tests (served on
localhost) and Integration (served by the stand-in core, fake-core-broker.yaml).

It speaks booth-spark's published shapes (internal/api/openapi/v1.yaml at booth-spark@dfbb124) where the
runner depends on them, and RUNS what it is given: ``main.inlinePython`` is executed by a real Python
process, with a stub ``pyspark`` on its path (``pyspark/`` beside this file), so the runner's prelude,
the task's code, ``ctx`` and the output line are exercised for real. A run goes pending -> running ->
succeeded | failed | stopped; its log (stdout and stderr) is served with the log cursor booth-spark is
adding (``?from=<offset>``, next offset in ``X-Log-Offset``; ADR 0119 amendment 3 point 7) and with
``?tail=``. Not modelled: data access, resources, admission beyond a settable capacity.

Where it is NOT the real contract, and how that is caught: tests/integration/spark-real.sh runs the same
calls against booth-spark's published chart and image. ``GET /v1/limits`` here is provisional: it was in
booth-spark's design-v0.md but not built in 0.3.0 (gate 3); booth-spark adds it in 0.3.1, and this body is
to be replaced by the real one then, which spark-real.sh compares shape for shape (SPARK_HAS_LIMITS=1).
Real booth-spark also expects X-Booth-Workspace (core's gateway sets it from X-Workspace); this stand-in
plays gateway and module in one, so it reads X-Workspace.

Test-only routes (never on booth-spark): ``GET /_standin/apps`` (every run, with its timestamps and
Idempotency-Key) and ``POST /_standin/control`` (``{"capacity": n}`` for 429 at_capacity).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PREFIX = "/modules/spark"
HERE = Path(__file__).resolve().parent
CONF_ALLOWED = {
    "spark.sql.shuffle.partitions",
    "spark.default.parallelism",
    "spark.sql.adaptive.enabled",
    "spark.sql.session.timeZone",
    "spark.sql.ansi.enabled",
    "spark.sql.execution.arrow.pyspark.enabled",
}
LIVE = ("pending", "running")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _duration(s: str) -> float:
    units = {"s": 1, "m": 60, "h": 3600}
    return float(s[:-1]) * units[s[-1]] if s and s[-1] in units else float(s or 3600)


class App:
    def __init__(self, workspace: str, spec: dict, key: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.workspace, self.spec, self.key = workspace, spec, key
        self.state, self.reason = "pending", ""
        self.created_at, self.started_at, self.finished_at = _now(), None, None
        self.stop_requested_at: str | None = None
        self.log = bytearray()
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def view(self) -> dict:
        main = self.spec.get("main") or {}
        return {
            "id": self.id, "kind": "application", "workspace": self.workspace, "name": self.spec.get("name"),
            "state": self.state, "reason": self.reason or None, "stopRequested": bool(self.stop_requested_at),
            "createdAt": self.created_at, "startedAt": self.started_at, "finishedAt": self.finished_at,
            "workload": True, "submitter": "pipeline", "footprintMi": 0,
            "resources": {"driverMemory": "1g", "executorMemory": "1g", "minExecutors": 0, "maxExecutors": 0, "maxDuration": self.spec.get("maxDuration", "")},
            "main": {"inlinePythonBytes": len(main.get("inlinePython", "").encode())},
        }  # fmt: skip


class StandIn:
    def __init__(self, python: str = sys.executable, start_delay: float = 1.0) -> None:
        self.python, self.start_delay = python, start_delay
        self.apps: dict[str, App] = {}
        self.capacity: int | None = None
        self.lock = threading.Lock()

    # ---- running an application ----
    def _run(self, app: App) -> None:
        time.sleep(self.start_delay)
        with app.lock:
            if app.state != "pending":
                return
            d = tempfile.mkdtemp(prefix="standin-")
            Path(d, "main.py").write_text(app.spec["main"]["inlinePython"], encoding="utf-8")
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(HERE), "PYTHONUNBUFFERED": "1", "HOME": d}
            app.proc = subprocess.Popen(
                [self.python, "main.py", *app.spec.get("args", [])], cwd=d, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            )  # fmt: skip
            app.state, app.started_at = "running", _now()
        deadline = time.monotonic() + _duration(app.spec.get("maxDuration", "3600s"))
        reader = threading.Thread(target=self._pump, args=(app,), daemon=True)
        reader.start()
        while app.proc.poll() is None:
            if time.monotonic() > deadline:
                app.proc.kill()
                with app.lock:
                    app.reason = "the run reached its maxDuration"
            time.sleep(0.2)
        reader.join(timeout=5)
        with app.lock:
            if app.state == "running":
                if app.stop_requested_at:
                    app.state, app.reason = "stopped", "stopped on request"
                elif app.proc.returncode == 0:
                    app.state = "succeeded"
                else:
                    app.state = "failed"
                    app.reason = app.reason or f"the driver exited with code {app.proc.returncode}"
            app.finished_at = _now()

    def _pump(self, app: App) -> None:
        assert app.proc and app.proc.stdout
        for chunk in iter(lambda: app.proc.stdout.read1(4096), b""):
            with app.lock:
                app.log += chunk

    # ---- the API ----
    def route(self, method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
        u = urlsplit(path)
        p, q = u.path, parse_qs(u.query)
        if p.startswith(PREFIX):
            p = p[len(PREFIX) :]
        h = {k.lower(): v for k, v in headers.items()}
        if p == "/_standin/apps":
            with self.lock:
                return self._json(200, [{**a.view(), "idempotencyKey": a.key, "stopRequestedAt": a.stop_requested_at} for a in self.apps.values()])
        if p == "/_standin/control" and method == "POST":
            self.capacity = json.loads(body or b"{}").get("capacity")
            return self._json(200, {"capacity": self.capacity})
        if not h.get("authorization", "").startswith("Bearer "):
            return self._json(401, {"error": "a bearer token is required", "code": "unauthenticated"})
        ws = h.get("x-workspace", "")
        if p == "/v1/limits" and method == "GET":
            return self._json(200, {"maxExecutors": 2, "maxMemory": "4g", "maxDuration": "2h", "canSubmit": True})
        if p == "/v1/applications" and method == "POST":
            return self._submit(ws, h.get("idempotency-key", ""), json.loads(body or b"{}"))
        parts = p.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["v1", "applications"]:
            with self.lock:
                app = self.apps.get(parts[2])
            if app is None or app.workspace != ws:
                return self._json(404, {"error": "no such run", "code": "not_found"})
            if len(parts) == 3 and method == "GET":
                with app.lock:
                    return self._json(200, app.view())
            if parts[3:] == ["stop"] and method == "POST":
                return self._stop(app)
            if parts[3:] == ["logs"] and method == "GET":
                with app.lock:
                    data = bytes(app.log)
                if "from" in q:
                    start = int(q["from"][0])
                    return 200, {"Content-Type": "text/plain", "X-Log-Offset": str(len(data))}, data[start:]
                tail = int(q.get("tail", ["500"])[0])
                return 200, {"Content-Type": "text/plain"}, b"\n".join(data.split(b"\n")[-tail:])
        return self._json(404, {"error": "not found", "code": "not_found"})

    def _submit(self, ws: str, key: str, spec: dict) -> tuple[int, dict[str, str], bytes]:
        with self.lock:
            if key:
                for a in self.apps.values():
                    if a.key == key and a.workspace == ws:
                        return self._json(200, a.view())
            main = spec.get("main") or {}
            if not isinstance(main.get("inlinePython"), str):
                return self._json(422, {"error": "main.inlinePython is required here", "code": "invalid"})
            if len(main["inlinePython"].encode()) > 262144:
                return self._json(400, {"error": "main.inlinePython is more than 256 KiB", "code": "invalid"})
            bad = sorted(set(spec.get("conf") or {}) - CONF_ALLOWED)
            if bad:
                return self._json(400, {"error": f"conf {bad[0]} is not one a caller may set", "code": "conf_not_allowed"})
            live = sum(1 for a in self.apps.values() if a.state in LIVE)
            if self.capacity is not None and live >= self.capacity:
                return self._json(429, {"error": f"at capacity: runs.maxRunning {self.capacity}", "code": "at_capacity"})
            app = App(ws, spec, key)
            self.apps[app.id] = app
        threading.Thread(target=self._run, args=(app,), daemon=True).start()
        return self._json(201, app.view())

    def _stop(self, app: App) -> tuple[int, dict[str, str], bytes]:
        with app.lock:
            if app.state not in LIVE:
                return self._json(200, app.view())
            app.stop_requested_at = _now()
            if app.state == "pending":
                app.state, app.reason, app.finished_at = "stopped", "stopped on request", _now()
            elif app.proc:
                app.proc.kill()
            return self._json(202, app.view())

    @staticmethod
    def _json(status: int, body: object) -> tuple[int, dict[str, str], bytes]:
        return status, {"Content-Type": "application/json"}, json.dumps(body).encode()


def handler_for(standin: StandIn):
    class H(BaseHTTPRequestHandler):
        def _do(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            status, headers, raw = standin.route(self.command, self.path, dict(self.headers), body)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = _do

        def log_message(self, *a) -> None:
            pass

    return H


def serve(standin: StandIn, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), handler_for(standin))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
