"""Drives real task Jobs from INSIDE the API/scheduler pod (ADR 0096) — for .github/workflows/integration.yml.

Piped into the running API pod, so it uses that pod's real ServiceAccount token, its real namespace
Role, the chart's real Job template, and the CNI's real NetworkPolicy enforcement:

    kubectl -n booth-pipeline exec -i deploy/booth-pipeline -- python - <mode> [args] < tests/integration/task_jobs_driver.py

Modes (each prints a JSON report and exits non-zero on any violation):

* ``isolation`` — the required isolation test (docs/decisions/0018): two CONCURRENT tasks from
  different workspaces, each trying to reach the other's token, Secret, pod and logs via
  (1) the original attack — scanning /tmp and every /proc/<pid>/environ and cmdline it can see;
  (2) the Kubernetes API — anonymously, since a task pod has no ServiceAccount token;
  (3) a direct connection to the other task's pod on its runner port.
* ``coldstart N`` — N sequential trivial tasks; reports Job-created -> pod-Ready latency.
* ``probe HOST:PORT ...`` — a task that tries a TCP connect to each address; reports which connected.
* ``sleep N`` — one task that just sleeps N seconds (a live task pod to aim at).
* ``refresh`` — how long a refreshed platform token takes to reach a running task.
* ``db WORKSPACE ROLE|none`` — a task's view of the database: DATABASE_URL, what it can reach, and a
  real round trip through the credential sidecar when it has one (ADR 0095).
* ``rotation WORKSPACE`` — a connection held open through the sidecar survives its renewal onto a
  new lease.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import sys
import threading
import time
import uuid

from booth_pipeline.runners.base import Cancellation, TaskAccess, TaskInvocation
from booth_pipeline.runners.kubejob import TASK_JOB_LABEL, KubernetesJobRunner

TEMPLATE = os.environ.get("BOOTH_PIPELINE_TASK_JOB_TEMPLATE", "/etc/booth/task-job/job.json")


class Lines:
    def __init__(self) -> None:
        self.out: list[tuple[str, str]] = []

    def line(self, stream: str, message: str) -> None:
        self.out.append((stream, message))

    def ready_seconds(self) -> float:
        for s, m in self.out:
            found = re.match(r"task pod ready in ([0-9.]+)s", m)
            if s == "system" and found:
                return float(found.group(1))
        raise AssertionError(f"no 'task pod ready' line in {self.out}")


def runner(**kw) -> KubernetesJobRunner:
    return KubernetesJobRunner.from_template_file(TEMPLATE, name_prefix=os.environ.get("BOOTH_PIPELINE_TASK_NAME_PREFIX", "booth-pipeline-task"), **kw)


def inv(source: str, key: str, params: dict | None = None, access: TaskAccess | None = None, timeout: int = 300) -> TaskInvocation:
    return TaskInvocation(f"ci-{uuid.uuid4().hex[:8]}", key, "transform", 1, source, params or {}, {}, timeout, access)


# What each isolation task runs, inside its own Job pod. It knows its own workspace's token prefix
# (to prove the scan works — it must find its OWN token) and looks for anyone else's.
ATTACKER = r'''
import glob, json, os, socket, ssl, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

def run(ctx):
    me = ctx.params["workspace"]
    started = time.time()
    own_token = open(os.environ["BOOTH_TOKEN_FILE"]).read()
    found = {"own": False, "foreign": []}

    def look(text, where):
        for tok in set(t for t in text.split() if t.startswith("tok-")):
            if tok.startswith(f"tok-{me}-"):
                found["own"] = True
            else:
                found["foreign"].append(where)

    # Give the other task time to be running before attacking it (the driver checks the overlap).
    time.sleep(ctx.params["settle"])

    # (1) The original attack: every file under /tmp, every process's environ and cmdline.
    # os.walk, not glob: glob skips dotfiles, and the token file is one (.booth-platform-token).
    for root, _, files in os.walk("/tmp"):
        for name in files:
            path = os.path.join(root, name)
            try:
                look(open(path, errors="replace").read(), path)
            except OSError:
                pass
    pids = [p for p in os.listdir("/proc") if p.isdigit()]
    for pid in pids:
        for f in ("environ", "cmdline"):
            try:
                look(open(f"/proc/{pid}/{f}", "rb").read().replace(b"\0", b" ").decode(errors="replace"), f"/proc/{pid}/{f}")
            except OSError:
                pass
    runner_auth = sorted(glob.glob("/etc/booth/runner-auth/*")) + sorted(glob.glob("/var/run/secrets/**", recursive=True))

    # (2) The Kubernetes API, as the task: no ServiceAccount token is mounted, so anonymously.
    host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    ns = ctx.params["namespace"]
    api = {}
    insecure = ssl.create_default_context(); insecure.check_hostname = False; insecure.verify_mode = ssl.CERT_NONE
    for path in (f"/api/v1/namespaces/{ns}/secrets", f"/api/v1/namespaces/{ns}/pods", f"/apis/batch/v1/namespaces/{ns}/jobs"):
        try:
            with urllib.request.urlopen(f"https://{host}:{port}{path}", timeout=5, context=insecure) as r:
                api[path] = r.status
        except urllib.error.HTTPError as e:
            api[path] = e.code
        except OSError as e:
            api[path] = f"no connection ({e.__class__.__name__})"

    # (3) A direct connection to the other task's pod: every address in this pod's /24 (which on a
    # single-node cluster holds every pod, the other task's included), on the runner port.
    mine = socket.gethostbyname(socket.gethostname())
    net = mine.rsplit(".", 1)[0]
    def connect(ip):
        try:
            socket.create_connection((ip, 8080), timeout=1.5).close()
            return ip
        except OSError:
            return None
    with ThreadPoolExecutor(64) as pool:
        reached = [ip for ip in pool.map(connect, [f"{net}.{i}" for i in range(1, 255)]) if ip and ip != mine]

    time.sleep(ctx.params["linger"])  # stay up while the other task attacks THIS pod
    return {"workspace": me, "ip": mine, "started": started, "ended": time.time(), "own_token_found": found["own"],
            "foreign_tokens_at": found["foreign"], "pids_visible": len(pids), "secret_files": runner_auth,
            "kube_api": api, "reached_on_8080": reached, "own_token_prefix_ok": own_token.startswith(f"tok-{me}-")}
'''


def isolation() -> int:
    ns = open("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read().strip()
    r = runner(max_concurrent=2)
    results: dict[str, dict] = {}
    logs: dict[str, Lines] = {}
    errors: dict[str, str] = {}

    def task(ws: str) -> None:
        n = iter(range(2, 1000))
        access = TaskAccess(ws, f"tok-{ws}-1-{uuid.uuid4().hex}", "http://unused", "http://unused", refresh=lambda: f"tok-{ws}-{next(n)}-{uuid.uuid4().hex}")
        logs[ws] = Lines()
        try:
            results[ws] = r.run(inv(ATTACKER, f"attack-{ws}", {"workspace": ws, "namespace": ns, "settle": 8, "linger": 15}, access), logs[ws], Cancellation())
        except Exception as e:  # noqa: BLE001
            errors[ws] = f"{e.__class__.__name__}: {e}"

    threads = [threading.Thread(target=task, args=(ws,)) for ws in ("acme", "globex")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(600)
    report = {"results": results, "errors": errors, "logs": {ws: ln.out for ws, ln in logs.items()}}
    print(json.dumps(report, indent=2))
    problems = list(errors.values())
    if len(results) == 2:
        a, b = results["acme"], results["globex"]
        if not (a["started"] < b["ended"] and b["started"] < a["ended"]):
            problems.append("the two tasks did not overlap in time, so this proved nothing")
        for ws, res in results.items():
            other = b if ws == "acme" else a
            if not res["own_token_found"] or not res["own_token_prefix_ok"]:
                problems.append(f"{ws}: control failed — the scan did not find the task's OWN token, so it cannot vouch for not finding another's")
            if res["foreign_tokens_at"]:
                problems.append(f"{ws}: read another workspace's token at {res['foreign_tokens_at']}")
            if [f for f in res["secret_files"] if not f.startswith("/etc/booth/runner-auth/")]:
                problems.append(f"{ws}: has a ServiceAccount token mounted: {res['secret_files']}")
            if [p for p, status in res["kube_api"].items() if status == 200]:
                problems.append(f"{ws}: the Kubernetes API answered it: {res['kube_api']}")
            if other["ip"] in res["reached_on_8080"]:
                problems.append(f"{ws}: connected to the other task's pod ({other['ip']}:8080)")
            if res["reached_on_8080"]:
                problems.append(f"{ws}: connected to pods it should have no route to: {res['reached_on_8080']}")
    print("\n".join(["ISOLATION VIOLATIONS:", *problems]) if problems else "isolation: OK — neither task reached the other's token, Secret, pod or logs")
    return 1 if problems else 0


def coldstart(n: int) -> int:
    r = runner(max_concurrent=1)
    times = []
    for i in range(n):
        lines = Lines()
        t0 = time.monotonic()
        assert r.run(inv("def run(ctx):\n    return 'ok'\n", f"cold-{i}"), lines, Cancellation()) == "ok"
        times.append({"ready_s": lines.ready_seconds(), "total_s": round(time.monotonic() - t0, 2)})
    ready = [t["ready_s"] for t in times]
    # The baseline it replaces: the same trivial task as a bare subprocess (the old runner's path).
    from booth_pipeline.runners.subprocess_runner import SubprocessRunner

    sub = []
    for i in range(n):
        t0 = time.monotonic()
        SubprocessRunner().run(inv("def run(ctx):\n    return 'ok'\n", f"sub-{i}"), Lines(), Cancellation())
        sub.append(round(time.monotonic() - t0, 2))
    print(
        json.dumps(
            {
                "runs": times,
                "ready_min_s": min(ready),
                "ready_median_s": statistics.median(ready),
                "ready_max_s": max(ready),
                "subprocess_total_median_s": statistics.median(sub),
            },
            indent=2,
        )
    )
    return 0


REFRESH_WATCH = r'''
import os, time
def run(ctx):
    seen, last = [], None
    end = time.time() + ctx.params["seconds"]
    while time.time() < end:
        tok = open(os.environ["BOOTH_TOKEN_FILE"]).read()
        if tok != last:
            seen.append([tok, time.time()]); last = tok
        time.sleep(0.01)
    return seen
'''


def refresh() -> int:
    """Token refresh is a PUT over the task's connection; how long until the task sees the new token.
    (Pod clocks on one node; on a multi-node cluster this includes their NTP skew.)"""
    minted: dict[str, float] = {}
    n = iter(range(2, 1000))

    def mint() -> str:
        tok = f"tok-ci-{next(n)}"
        minted[tok] = time.time()
        return tok

    access = TaskAccess("ci", "tok-ci-1", "http://unused", "http://unused", refresh=mint)
    seen = runner(max_concurrent=1, refresh_seconds=1.0).run(inv(REFRESH_WATCH, "refresh", {"seconds": 8}, access), Lines(), Cancellation())
    lat = [round((at - minted[tok]) * 1000, 1) for tok, at in seen if tok in minted]
    assert lat, f"no refresh reached the task: {seen}"
    print(json.dumps({"refreshes_seen": len(lat), "latency_ms": lat, "median_ms": statistics.median(lat)}))
    return 0


PROBE = r'''
import socket
def run(ctx):
    out = {}
    for addr in ctx.params["addrs"]:
        host, port = addr.rsplit(":", 1)
        try:
            socket.create_connection((host, int(port)), timeout=5).close()
            out[addr] = True
        except OSError:
            out[addr] = False
    return out
'''


def probe(addrs: list[str]) -> int:
    print(json.dumps(runner(max_concurrent=1).run(inv(PROBE, "probe", {"addrs": addrs}), Lines(), Cancellation())))
    return 0


DB_TASK = r'''
import os, socket
def run(ctx):
    def tcp(host, port):
        try:
            socket.create_connection((host, port), timeout=5).close()
            return True
        except OSError:
            return False
    out = {
        "database_url": os.environ.get("DATABASE_URL"),
        "sidecar_listening": tcp("127.0.0.1", 5432),
        "booth_database_direct": tcp("booth-database-postgres.booth-database.svc.cluster.local", 5432),
        "own_module_database": tcp("postgres.booth-pipeline.svc.cluster.local", 5432),
    }
    if out["database_url"]:
        import psycopg
        with psycopg.connect(out["database_url"], connect_timeout=10) as c:
            c.execute("CREATE TABLE IF NOT EXISTS ci_rows (n int)")
            c.execute("INSERT INTO ci_rows VALUES (42)")
            out["rows"] = c.execute("SELECT count(*) FROM ci_rows WHERE n = 42").fetchone()[0]
            out["session_user"], out["database"] = c.execute("SELECT session_user, current_database()").fetchone()
    return out
'''


def db(workspace: str, role: str) -> int:
    """One task, as ``workspace`` with ``role`` (or with no platform access when role is "none"),
    reporting what it can reach and, through DATABASE_URL if it has one, a real round trip."""
    access = None if role == "none" else TaskAccess(workspace, f"tok-{workspace}-{uuid.uuid4().hex}", "http://unused", "http://unused", role=role)
    lines = Lines()
    try:
        out = runner(max_concurrent=1).run(inv(DB_TASK, "db", {}, access), lines, Cancellation())
    except Exception:
        print("\n".join(f"[{s}] {m}" for s, m in lines.out), file=sys.stderr)  # the task's own traceback
        raise
    out["system"] = [m for s, m in lines.out if s == "system"]
    print(json.dumps(out))
    return 0


# Holds one connection open through the sidecar while the sidecar renews onto a NEW lease (a new
# Postgres role), then proves that connection is still the same live backend.
ROTATION = r'''
import os, time
import psycopg
def run(ctx):
    url = os.environ["DATABASE_URL"]
    held = psycopg.connect(url, autocommit=True)
    first_user, first_pid = held.execute("SELECT session_user, pg_backend_pid()").fetchone()
    deadline = time.time() + ctx.params["within"]
    rotated_to = None
    while time.time() < deadline and rotated_to is None:
        time.sleep(1)
        with psycopg.connect(url, connect_timeout=10) as fresh:  # a NEW connection must never fail meanwhile
            user = fresh.execute("SELECT session_user").fetchone()[0]
        if user != first_user:
            rotated_to = user
    still_user, still_pid = held.execute("SELECT session_user, pg_backend_pid()").fetchone()
    held.execute("SELECT 1")
    held.close()
    return {"first_user": first_user, "rotated_to": rotated_to, "held_user": still_user,
            "same_backend": still_pid == first_pid, "held_pid": first_pid}
'''


def rotation(workspace: str) -> int:
    access = TaskAccess(workspace, f"tok-{workspace}-{uuid.uuid4().hex}", "http://unused", "http://unused", role="editor")
    out = runner(max_concurrent=1).run(inv(ROTATION, "rotation", {"within": 90}, access, timeout=300), Lines(), Cancellation())
    print(json.dumps(out))
    ok = out["rotated_to"] and out["same_backend"] and out["held_user"] == out["first_user"]
    print("rotation: OK — the held connection survived the sidecar's renewal onto a new lease" if ok else "ROTATION FAILED")
    return 0 if ok else 1


def sleep(seconds: int) -> int:
    runner(max_concurrent=1).run(inv(f"import time\ntime.sleep({seconds})\n", "sleep"), Lines(), Cancellation())
    return 0


def main() -> int:
    mode, args = sys.argv[1], sys.argv[2:]
    if mode == "sleep":
        return sleep(int(args[0]))
    if mode == "isolation":
        return isolation()
    if mode == "coldstart":
        return coldstart(int(args[0]) if args else 5)
    if mode == "refresh":
        return refresh()
    if mode == "db":
        return db(args[0], args[1])
    if mode == "rotation":
        return rotation(args[0])
    if mode == "probe":
        return probe(args)
    raise SystemExit(f"unknown mode {mode!r}; expected isolation, coldstart or probe (label {TASK_JOB_LABEL})")


if __name__ == "__main__":
    sys.exit(main())
