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
* ``egress NAME=HOST:PORT ...`` — the same, but reports HOW each attempt ended (``open``, ``timeout``,
  ``refused`` or ``error:<Class>``) plus whether DNS resolves, for tests/integration/egress.sh.
* ``sleep N`` — one task that just sleeps N seconds (a live task pod to aim at).
* ``refresh`` — how long a refreshed platform token takes to reach a running task.
* ``db WORKSPACE ROLE|none`` — a task's view of the database: DATABASE_URL, what it can reach, and a
  real round trip through the credential sidecar when it has one (ADR 0095).
* ``s3 WORKSPACE ROLE|none [WATCH_SECONDS]`` — a task's view of the s3 credential sidecar: the AWS
  file variables, the keys it wrote, whether the backend is reachable; optionally waits for a renewal.
* ``duckdb WORKSPACE`` — DuckDB through the s3 sidecar: a Python task writes Parquet with ctx.s3,
  a SQL task reads it back with no setup.
* ``rotation WORKSPACE`` — a connection held open through the sidecar survives its renewal onto a
  new lease.
* ``pip-probe NAME=HOST:PORT ...`` — ADR 0115: a task requiring ``booth-pip-probe``, whose sdist build
  (in the pod's pip init container) connects to each target; the task's own code then connects to the
  same targets. Reports ``init:NAME`` and ``task:NAME`` verdicts, as ``egress`` does. The targets must
  also be in the index pod's /tmp/probe-targets.json (tests/integration/pip.sh writes them).
* ``pip-secrets WORKSPACE ROLE`` — ADR 0115: what the pip init container can see in a task pod that has
  a platform token and a credential sidecar: mounts, environment, the sidecars' ports, and a scan for
  the task's runner bearer and platform token. Then the same pod's task checks it does have both.
* ``pip-run REQUIREMENT ...`` — a trivial task with these requirements; reports how it ended.
* ``pip-scrape PAGE_URL`` — a task requiring requests and beautifulsoup4 fetches and parses a page.
* ``spark-e2e`` — ADR 0119/0120: scheduled runs of pipelines with a Spark task, through THIS pod's own
  service (its store, engine, Job runner and Spark runner, built from its environment), against the
  stand-in core's mint and stand-in booth-spark (fake-core-broker.yaml, fixtures/spark/): output handed
  downstream, the driver log and state in the run log, the attempt's Spark record, a refused mint for a
  disabled service account and for a person owner, cancel stopping the application, and a task pod
  killed mid-run whose application is stopped before the retry, which waits the floor.
* ``spark-real`` — the same calls against the REAL booth-spark (tests/integration/spark-real.sh): /v1/me
  with person and workload tokens, pipeline's availability probe (/v1/limits) and, where booth-spark has
  it, the stand-in's /v1/limits shape against the real one; a scheduled Spark task run by real Spark;
  booth-spark's own refusal; cancel.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import statistics
import sys
import threading
import time
import uuid

from booth_pipeline.runners import kubejob
from booth_pipeline.runners.base import Cancellation, TaskAccess, TaskFailed, TaskInvocation
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


EGRESS = r'''
import socket
from concurrent.futures import ThreadPoolExecutor
def run(ctx):
    def attempt(addr):
        host, port = addr.rsplit(":", 1)
        try:
            socket.create_connection((host, int(port)), timeout=ctx.params["timeout"]).close()
            return "open"
        except (TimeoutError, socket.timeout):
            return "timeout"
        except ConnectionRefusedError:
            return "refused"
        except OSError as e:
            return f"error:{e.__class__.__name__}:{e.errno}"
    targets = ctx.params["targets"]
    with ThreadPoolExecutor(len(targets)) as pool:
        out = dict(zip(targets, pool.map(attempt, targets.values())))
    try:
        out["dns"] = "resolves" if socket.gethostbyname("kubernetes.default.svc.cluster.local") else "no"
    except OSError as e:
        out["dns"] = f"error:{e.__class__.__name__}"
    return out
'''


def egress(pairs: list[str]) -> int:
    targets = dict(p.split("=", 1) for p in pairs)
    print(json.dumps(runner(max_concurrent=1).run(inv(EGRESS, "egress", {"targets": targets, "timeout": 5}), Lines(), Cancellation())))
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


S3_TASK = r'''
import configparser, os, socket, time
def run(ctx):
    def tcp(host, port):
        try:
            socket.create_connection((host, port), timeout=5).close()
            return True
        except OSError:
            return False
    creds, conf = os.environ.get("AWS_SHARED_CREDENTIALS_FILE"), os.environ.get("AWS_CONFIG_FILE")
    out = {"credentials_file": creds, "config_file": conf,
           "backend_reachable": tcp("s3-backend.booth-storage-ci.svc.cluster.local", 9000),
           "sidecar_health": tcp("127.0.0.1", 8081)}
    if creds:
        p = configparser.ConfigParser()
        p.read(creds)
        out["access_key_id"] = p.get("default", "aws_access_key_id", fallback=None)
        out["has_secret"] = bool(p.get("default", "aws_secret_access_key", fallback=""))
        out["config_written"] = os.path.exists(conf)  # <credentials>.config: endpoint_url, region, addressing_style
        if out["config_written"]:
            c = configparser.ConfigParser()
            c.read(conf)
            out["config"] = {s: dict(c[s]) for s in c.sections()}
    if ctx.params.get("watch_rotation"):
        # The sidecar renews and rewrites the file atomically: every read parses, and the key changes.
        first, seen, deadline = out["access_key_id"], set(), time.time() + ctx.params["watch_rotation"]
        while time.time() < deadline:
            p = configparser.ConfigParser()
            p.read(creds)
            key = p.get("default", "aws_access_key_id")  # raises on a torn/partial file
            seen.add(key)
            if key != first:
                break
            time.sleep(0.2)
        out["rotated_to"] = next((k for k in seen if k != first), None)
    return out
'''


def s3(workspace: str, role: str, watch_rotation: int = 0) -> int:
    """One task, as ``workspace`` with ``role`` (or "none"), reporting what it sees of the s3
    credential sidecar (ADR 0095): the AWS file variables, the keys it wrote, the backend's reachability."""
    access = None if role == "none" else TaskAccess(workspace, f"tok-{workspace}-{uuid.uuid4().hex}", "http://unused", "http://unused", role=role)
    lines = Lines()
    try:
        out = runner(max_concurrent=1).run(inv(S3_TASK, "s3", {"watch_rotation": watch_rotation}, access, timeout=300), lines, Cancellation())
    except Exception:
        print("\n".join(f"[{s}] {m}" for s, m in lines.out), file=sys.stderr)
        raise
    out["system"] = [m for s, m in lines.out if s == "system"]
    print(json.dumps(out))
    return 0


DUCKDB_WRITE = r'''
import duckdb
def run(ctx):
    con = duckdb.connect(":memory:")
    loc = ctx.s3.duckdb(con)  # the helper: location from the sidecar's config file, keys from its credentials
    url = ctx.params["url"]
    con.execute(f"COPY (SELECT 42 AS answer, 'acme' AS ws) TO '{url}' (FORMAT parquet)")
    back = con.execute(f"SELECT answer FROM '{url}'").fetchone()[0]
    return {"location": [loc.endpoint_url, loc.region, loc.addressing_style], "read_back": back}
'''


def duckdb_e2e(workspace: str) -> int:
    """DuckDB through the credential sidecar, both ways a task uses it (ADR 0095, docs/decisions/0019):
    a Python task writes Parquet to the real MinIO with ctx.s3, then a SQL task (its own pod, its own
    lease) reads it back with no setup at all. Both run with no internet: the extensions are baked."""
    url = f"s3://booth-ci/lakehouse/{workspace}/ci-{uuid.uuid4().hex[:8]}.parquet"

    def access() -> TaskAccess:
        return TaskAccess(workspace, f"tok-{workspace}-{uuid.uuid4().hex}", "http://unused", "http://unused", role="editor")

    out: dict = {}
    for name, source, language in (("python", DUCKDB_WRITE, "python"), ("sql", "SELECT answer, ws FROM read_parquet($url)", "sql")):
        lines = Lines()
        i = TaskInvocation(f"ci-{uuid.uuid4().hex[:8]}", f"duckdb_{name}", "transform", 1, source, {"url": url}, {}, 300, access(), language=language)
        try:
            out[name] = runner(max_concurrent=1).run(i, lines, Cancellation())
        except Exception:
            print("\n".join(f"[{s}] {m}" for s, m in lines.out), file=sys.stderr)
            raise
        out[name + "_log"] = [m for s, m in lines.out if s != "stdout"]
    print(json.dumps(out))
    ok = out["python"]["read_back"] == 42 and out["sql"] == [{"answer": 42, "ws": workspace}]
    print("duckdb s3: OK — written by a Python task with ctx.s3, read back by a SQL task" if ok else "DUCKDB S3 FAILED")
    return 0 if ok else 1


# ---- ADR 0115: a task's own Python packages -------------------------------------------------------


def pip_run(reqs: list[str], source: str, params: dict | None = None, access: TaskAccess | None = None) -> dict:
    """One task with requirements, through the chart's real template and this pod's real pip route."""
    lines = Lines()
    task = dataclasses.replace(inv(source, "pip", params, access), requirements=tuple(reqs))
    started = time.monotonic()
    out, error = None, None
    try:
        out = runner(max_concurrent=1, pip_route=os.environ.get("BOOTH_PIPELINE_PIP_ROUTE", "")).run(task, lines, Cancellation())
    except TaskFailed as e:
        error = str(e)
    return {
        "output": out,
        "error": error,
        "seconds": round(time.monotonic() - started, 1),
        "system": [m for s, m in lines.out if s == "system"],
        "stdout": [m for s, m in lines.out if s == "stdout"],
    }


PIP_PROBE_TASK = EGRESS.replace("def run(ctx):", "import booth_pip_probe\ndef run(ctx):").replace(
    "    return out\n", '    return {"init": booth_pip_probe.RESULT["verdicts"], "task": out}\n'
)


def pip_probe(pairs: list[str]) -> int:
    targets = dict(p.split("=", 1) for p in pairs)
    r = pip_run(["booth-pip-probe"], PIP_PROBE_TASK, {"targets": targets, "timeout": 5})
    if r["error"]:
        print(json.dumps(r))  # pip.sh's verdict checks then fail as "missing", with this shown above them
        return 0
    out = {f"init:{k}": v for k, v in r["output"]["init"].items()}
    out.update({f"task:{k}": v for k, v in r["output"]["task"].items()})
    print(json.dumps(out))
    return 0


PIP_SECRETS_TASK = r'''
import hashlib, os
import booth_pip_probe
def h(t):
    return hashlib.sha256(t.encode()).hexdigest()[:16]
def run(ctx):
    r = dict(booth_pip_probe.RESULT)
    found = set(r.pop("token_hashes"))
    r["found"] = {name: digest in found for name, digest in ctx.params["hashes"].items()}
    # The same pod's task, after the install: it does hold its token and its database lease.
    token_file = os.environ.get("BOOTH_TOKEN_FILE", "")
    r["task_has_its_token"] = bool(token_file) and h(open(token_file).read()) == ctx.params["hashes"]["platform_token"]
    r["task_database_url"] = os.environ.get("DATABASE_URL")
    if r["task_database_url"]:
        import psycopg
        with psycopg.connect(r["task_database_url"], connect_timeout=10) as c:
            r["task_database_rows"] = c.execute("SELECT 1").fetchone()[0]
    return r
'''


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def pip_secrets(workspace: str, role: str) -> int:
    token = f"tok-{workspace}-{uuid.uuid4().hex}"
    bearer = kubejob.secrets.token_urlsafe(32)
    # The runner's per-task bearer is random; make it one this test knows, so the scan can look for it.
    kubejob.secrets = type("FixedBearer", (), {"token_urlsafe": staticmethod(lambda n=32: bearer)})
    access = TaskAccess(workspace, token, "http://unused", "http://unused", role=role)
    r = pip_run(["booth-pip-probe"], PIP_SECRETS_TASK, {"hashes": {"bearer": _h(bearer), "platform_token": _h(token)}}, access)
    print(json.dumps(r))
    o = r["output"] or {}
    forbidden_mounts = [m for m in o.get("mounts", []) if m.startswith(("/etc/booth", "/var/run/booth-sidecar", "/var/run/secrets", "/tmp"))]
    forbidden_env = [n for n in o.get("env_names", []) if n.startswith(("BOOTH_", "AWS_")) or n == "DATABASE_URL"]
    checks = {
        "the task ran": r["error"] is None,
        "the install's mounts are only its own volume and the kubelet's files": "/opt/booth/site" in o.get("mounts", []) and not forbidden_mounts,
        "no runner, sidecar or AWS variable, nothing JWT-shaped in its environment": not forbidden_env and o.get("jwt_like_env_values") == 0,
        "no ServiceAccount token": o.get("service_account_token") is False,
        "no credential sidecar running yet (127.0.0.1:5432 and :8081 refused)": o.get("sidecar_ports") == {"5432": "refused", "8081": "refused"},
        "the scan works: it found its own decoy": o.get("decoy_found") is True,
        "the scan did not find the runner bearer": o.get("found", {}).get("bearer") is False,
        "the scan did not find the platform token": o.get("found", {}).get("platform_token") is False,
        "the same pod's task holds its token": o.get("task_has_its_token") is True,
        "the same pod's task reaches its database through the sidecar": o.get("task_database_rows") == 1,
    }
    for what, ok in checks.items():
        print(("ok: " if ok else "FAIL: ") + what, file=sys.stderr)
    return 0 if all(checks.values()) else 1


PIP_TRIVIAL_TASK = "print('TASK-CODE-RAN')\n"


def pip_run_mode(reqs: list[str]) -> int:
    print(json.dumps(pip_run(reqs, PIP_TRIVIAL_TASK)))
    return 0


PIP_SCRAPE_TASK = r'''
import bs4, requests
def run(ctx):
    r = requests.get(ctx.params["page"], timeout=10)
    r.raise_for_status()
    soup = bs4.BeautifulSoup(r.text, "html.parser")
    return {
        "team": soup.find(id="team").get_text(),
        "scores": [int(td.get_text()) for td in soup.select("td.score")],
        "requests": [requests.__version__, requests.__file__],
        "bs4": [bs4.__version__, bs4.__file__],
    }
'''


def pip_scrape(page: str) -> int:
    print(json.dumps(pip_run(["requests", "beautifulsoup4"], PIP_SCRAPE_TASK, {"page": page})))
    return 0


# ---- ADR 0119/0120: the Spark runner, end to end through this pod's own service ---------------------

SPARK_AGG = """
def run(ctx):
    n = ctx.spark.range(ctx.params["n"]).count()
    print("counted", n, "rows on attempt", ctx.attempt)
    return {"rows": n}
"""
SPARK_USE = "def run(ctx):\n    print('downstream got', ctx.inputs['agg']['rows'])\n    return ctx.inputs['agg']['rows'] * 2\n"
SPARK_LONG = "import time\ndef run(ctx):\n    if ctx.attempt == 1:\n        print('first attempt: waiting to be interrupted', flush=True)\n        time.sleep(600)\n    return 'done on attempt %d' % ctx.attempt\n"


def spark_e2e() -> int:
    import urllib.request

    from booth_pipeline.app import create_app
    from booth_pipeline.auth import Identity
    from booth_pipeline.config import Config
    from booth_pipeline.kube import KubeClient
    from booth_pipeline.model import PipelineSpec, Schedule, TaskConfig, TaskRef
    from booth_pipeline.runners.kubejob import RUN_LABEL

    cfg = Config.from_env()
    app = create_app(cfg, start_scheduler=False)
    svc = app.state.service
    core = cfg.core_url
    owner = Identity("u-ci-owner", "ci-owner", "acme", "owner", "tok-ci-owner")
    checks: dict[str, bool] = {}
    report: dict[str, object] = {}

    def http(method: str, path: str, body: object = None):
        req = urllib.request.Request(core + path, data=None if body is None else json.dumps(body).encode(), method=method, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def apps() -> list[dict]:
        return http("GET", "/modules/spark/_standin/apps")

    def check(name: str, ok: bool) -> None:
        checks[name] = bool(ok)
        print(("ok: " if ok else "FAIL: ") + name, file=sys.stderr, flush=True)

    def pipeline(name: str, nodes: list[tuple[str, str, list[str], dict]]) -> str:
        refs = []
        for key, src, deps, cfg_kw in nodes:
            config = TaskConfig.model_validate({"code": {"type": "inline", "source": src}, **cfg_kw})
            t, _ = svc.create_task(owner, f"{name}-{key}", "", config, "")
            refs.append(TaskRef(key=key, task_id=t.id, task_version=1, depends_on=deps))
        p, _ = svc.create_pipeline(owner, f"{name}-{uuid.uuid4().hex[:6]}", "", PipelineSpec(tasks=refs), "")
        svc.update_schedule(owner, p.id, Schedule(cron="0 6 * * 1"), False, "editor", None)  # owner: ci-owner
        return p.id

    def start(pid: str) -> str:
        return svc.start_scheduled(svc.get_pipeline(owner, pid)).id

    def wait(run_id: str, timeout: float = 300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run, tasks = svc.get_run(owner, run_id)
            if run.status in ("succeeded", "failed", "canceled"):
                return run, {t.task_key: t for t in tasks}
            time.sleep(1)
        raise AssertionError(f"run {run_id} did not finish in {timeout}s")

    def wait_running(run_id: str, key: str, timeout: float = 180) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, tasks = svc.get_run(owner, run_id)
            ext = next((t.external for t in tasks if t.task_key == key), None) or {}
            if ext.get("state") == "running":
                return ext
            time.sleep(0.5)
        raise AssertionError(f"{key} of run {run_id} never reached running")

    def log_lines(run_id: str) -> list:
        return svc.logs(owner, run_id, None, 0, 5000)

    # 1. a scheduled run: Spark output handed downstream, the driver log and state in the run log
    pid = pipeline("spark-ok", [("agg", SPARK_AGG, [], {"runner": "spark", "spark": {}, "params": {"n": 21}}), ("use", SPARK_USE, ["agg"], {})])
    before = len(apps())
    run, tasks = wait(start(pid))
    lines = log_lines(run.id)
    mine = [a for a in apps()[before:] if a["idempotencyKey"].startswith(run.id)]
    report["ok"] = {"status": run.status, "error": run.error, "external": tasks["agg"].external, "apps": [a["idempotencyKey"] for a in mine]}
    check("a scheduled Spark task succeeds", run.status == "succeeded")
    check("its application ran on booth-spark, keyed by run, task and attempt", [a["idempotencyKey"] for a in mine] == [f"{run.id}/agg/1"])
    check("the attempt records its application", (tasks["agg"].external or {}).get("state") == "succeeded" and (tasks["agg"].external or {}).get("id") == mine[0]["id"] if mine else False)
    check("the driver log is in the run log as the spark stream", any(ln.stream == "spark" and "counted 21 rows on attempt 1" in ln.message for ln in lines))
    check("its state changes are system lines", any(ln.stream == "system" and ln.message == "spark: running → succeeded" for ln in lines))
    check("the output reached the downstream task", any(ln.task_key == "use" and "downstream got 21" in ln.message for ln in lines))
    check("the run keeps both tasks' outputs to show", tasks["agg"].output == {"value": {"rows": 21}} and tasks["use"].output == {"value": 42})
    check("the run was authorized at its start", any("authorized: core will issue this run's tokens" in ln.message for ln in lines))
    mints = [m for m in http("GET", "/mints") if m["subject"] == f"pipeline:acme:{run.id}"]
    check("its tokens name pipeline:acme:<runId> and the owner", bool(mints) and all(m["owner"] == "u-ci-owner" for m in mints))

    # 2. a disabled service account: the whole run fails at its start, nothing is submitted
    svc.set_owner(owner, pid, "svc:acme:nfl-weekly")
    http("POST", "/_control/mint", {"owner": "svc:acme:nfl-weekly", "code": "service_account_disabled"})
    before = len(apps())
    run, tasks = wait(start(pid))
    report["disabled"] = {"status": run.status, "error": run.error}
    check("a disabled account's scheduled run fails with its reason", run.status == "failed" and "service account svc:acme:nfl-weekly, is disabled" in (run.error or ""))
    check("and runs nothing", len(apps()) == before and all(t.status == "skipped" for t in tasks.values()))

    # 3. a refused person owner: one indistinguishable message, naming Take over
    svc.set_owner(owner, pid, "me")
    http("POST", "/_control/mint", {"owner": "u-ci-owner", "code": None})
    run, tasks = wait(start(pid))
    report["person"] = {"status": run.status, "error": run.error}
    check("a refused person owner's run fails with the one message", run.status == "failed" and "would not issue a token for ci-owner" in (run.error or "") and "Take over" in (run.error or ""))
    http("POST", "/_control/mint", {"clear": True})

    # 4. cancel stops the application
    pid = pipeline("spark-cancel", [("agg", SPARK_LONG, [], {"runner": "spark", "spark": {}})])
    run_id = start(pid)
    ext = wait_running(run_id, "agg")
    svc.cancel_run(owner, run_id)
    run, tasks = wait(run_id)
    time.sleep(3)
    app_ = next(a for a in apps() if a["id"] == ext["id"])
    report["cancel"] = {"status": run.status, "app": app_["state"]}
    check("a canceled run is canceled", run.status == "canceled")
    check("and its application was stopped", bool(app_["stopRequestedAt"]) and app_["state"] == "stopped")

    # 5. a task pod killed mid-run: its application is stopped before the retry, which waits the floor
    pid = pipeline("spark-retry", [("agg", SPARK_LONG, [], {"runner": "spark", "spark": {}, "retry": {"maxRetries": 1, "delaySeconds": 0}})])
    run_id = start(pid)
    ext = wait_running(run_id, "agg")
    kube = KubeClient.in_cluster()
    jobs = {p["metadata"]["labels"]["booth.projectbooth.io/task-job"] for p in kube.list_pods(f"{RUN_LABEL}={run_id}")}
    for name in jobs:
        kube.delete_job(name)  # the pod goes with its Job: the attempt loses its runner mid-stream
    run, tasks = wait(run_id, 600)
    mine = sorted((a for a in apps() if a["idempotencyKey"].startswith(run_id)), key=lambda a: a["createdAt"])
    report["retry"] = {"status": run.status, "error": run.error, "apps": [(a["idempotencyKey"], a["state"], a["stopRequestedAt"], a["createdAt"], a["finishedAt"]) for a in mine]}
    check("the retried run succeeds", run.status == "succeeded" and tasks["agg"].attempts == 2)
    if len(mine) == 2:
        a1, a2 = mine
        check("the lost attempt's application was stopped before the retry submitted", bool(a1["stopRequestedAt"]) and a1["stopRequestedAt"] <= a2["createdAt"] and a1["state"] == "stopped")
        from datetime import datetime as _dt

        gap = (_dt.fromisoformat(a2["createdAt"]) - _dt.fromisoformat(a1["stopRequestedAt"])).total_seconds()
        report["retry_gap_seconds"] = round(gap, 1)
        check(f"the retry waited the floor ({gap:.1f}s >= runner.spark.retryMinDelaySeconds)", gap >= float(os.environ.get("BOOTH_PIPELINE_SPARK_RETRY_MIN_DELAY_SECONDS", "0")))
    else:
        check("exactly two applications for two attempts", False)

    print(json.dumps(report, default=str))
    return 0 if all(checks.values()) else 1


def spark_real() -> int:
    """Pipeline's Spark calls against the real booth-spark behind the stand-in gateway (spark-real.sh)."""
    import urllib.error
    import urllib.request

    from booth_pipeline.app import create_app
    from booth_pipeline.auth import Identity
    from booth_pipeline.config import Config
    from booth_pipeline.model import PipelineSpec, Schedule, TaskConfig, TaskRef
    from booth_pipeline.runners.spark import SparkRunner

    cfg = Config.from_env()
    core = cfg.core_url
    checks: dict[str, bool] = {}
    report: dict[str, object] = {}

    def check(name: str, ok: bool) -> None:
        checks[name] = bool(ok)
        print(("ok: " if ok else "FAIL: ") + name, file=sys.stderr, flush=True)

    def call(method: str, path: str, body: object = None, token: str = "", ws: str = "acme") -> tuple[int, dict, object]:
        h = {"Content-Type": "application/json", "X-Workspace": ws}
        if token:
            h["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(core + path, data=None if body is None else json.dumps(body).encode(), method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw, status, headers = r.read(), r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            raw, status, headers = e.read(), e.code, dict(e.headers or {})
        try:
            return status, headers, json.loads(raw)
        except ValueError:
            return status, headers, raw.decode(errors="replace")

    person = call("POST", "/test-idp/token", {"sub": "u-ci-owner", "workspace": "acme", "role": "editor"})[2]["token"]

    def workload(subject: str) -> str:
        return call("POST", "/api/internal/workload-tokens", {"workspace": "acme", "subject": subject, "roleCeiling": "editor", "owner": "u-ci-owner"}, token="ci")[2]["token"]

    # 1. the gateway path and both kinds of token
    st, _, me = call("GET", "/modules/spark/v1/me", token=person)
    report["me_person"] = me
    check("a person's token reaches the real /v1 through the gateway path", st == 200 and me.get("workspace") == "acme" and me.get("role") == "editor" and me.get("workload") is False)
    st, _, me = call("GET", "/modules/spark/v1/me", token=workload("pipeline:acme:contract"))
    report["me_workload"] = me
    check("a pipeline workload token is accepted as a workload caller", st == 200 and me.get("workload") is True and me.get("subject") == "pipeline:acme:contract")

    # 2. pipeline's availability probe, exactly as /api/runners and a save run it
    reason = SparkRunner(None, core).availability(person, "acme")  # type: ignore[arg-type]
    st, _, limits = call("GET", "/modules/spark/v1/limits", token=person)
    report["probe"] = {"reason": reason, "limits_status": st, "limits": limits}
    if os.environ.get("SPARK_HAS_LIMITS") == "1":
        check("pipeline's availability probe finds the real booth-spark", reason is None and st == 200)
        call("POST", "/_control/spark", {"upstream": None})
        _, _, standin = call("GET", "/modules/spark/v1/limits", token=person)
        call("POST", "/_control/spark", {"upstream": "http://booth-spark.booth-spark.svc:8080"})

        def shape(v):
            return {k: shape(x) for k, x in sorted(v.items())} if isinstance(v, dict) else type(v).__name__

        report["limits_shapes"] = {"real": shape(limits), "standin": shape(standin)}
        check("the stand-in's /v1/limits has exactly the real body's shape", isinstance(limits, dict) and shape(limits) == shape(standin))
    else:
        # booth-spark 0.3.0: the route was designed but never built. Recorded, not passed silently:
        # the probe must say "not installed" here, which is the gate-3 finding, until the pin moves.
        check("known gap (booth-spark has no /v1/limits yet): the probe reports it, as gate 3 saw", st == 404 and reason is not None and "not installed" in reason)

    # 3. a scheduled run's Spark task, through pipeline's own client, run by real Spark
    app = create_app(cfg, start_scheduler=False)
    svc = app.state.service
    owner = Identity("u-ci-owner", "ci-owner", "acme", "owner", person)
    # The save checks availability, which 0.3.0 can't answer: register the runner as available for
    # this in-process app (only the save path's check; the run itself goes to real booth-spark).
    spark_runner = svc.registry.get("spark")
    if os.environ.get("SPARK_HAS_LIMITS") != "1":
        spark_runner.availability = lambda token, workspace: None

    def pipeline(name: str, nodes: list[tuple[str, str, list[str], dict]]) -> str:
        refs = []
        for key, src, deps, cfg_kw in nodes:
            t, _ = svc.create_task(owner, f"{name}-{key}", "", TaskConfig.model_validate({"code": {"type": "inline", "source": src}, **cfg_kw}), "")
            refs.append(TaskRef(key=key, task_id=t.id, task_version=1, depends_on=deps))
        p, _ = svc.create_pipeline(owner, f"{name}-{uuid.uuid4().hex[:6]}", "", PipelineSpec(tasks=refs), "")
        svc.update_schedule(owner, p.id, Schedule(cron="0 6 * * 1"), False, "editor", None)
        return p.id

    def wait(run_id: str, timeout: float = 900):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run, tasks = svc.get_run(owner, run_id)
            if run.status in ("succeeded", "failed", "canceled"):
                return run, {t.task_key: t for t in tasks}
            time.sleep(2)
        raise AssertionError(f"run {run_id} did not finish in {timeout}s")

    small = {"resources": {"driver": {"memory": "512m"}, "executors": {"min": 1, "max": 1, "memory": "512m"}}}
    pid = pipeline("real-ok", [("agg", SPARK_AGG, [], {"runner": "spark", "spark": small, "params": {"n": 21}, "timeoutSeconds": 900}), ("use", SPARK_USE, ["agg"], {})])
    run, tasks = wait(svc.start_scheduled(svc.get_pipeline(owner, pid)).id)
    lines = svc.logs(owner, run.id, None, 0, 5000)
    report["ok"] = {"status": run.status, "error": run.error, "external": tasks["agg"].external}
    check("real booth-spark runs a scheduled Spark task to success", run.status == "succeeded")
    check("the attempt records the real application", (tasks["agg"].external or {}).get("state") == "succeeded")
    check("the driver log came through the real log cursor", any(ln.stream == "spark" and "counted 21 rows on attempt 1" in ln.message for ln in lines))
    check("its real state changes are system lines", any(ln.stream == "system" and ln.message.startswith("spark: ") and "→ running" in ln.message for ln in lines))
    check("the output line was found and reached the downstream task", any(ln.task_key == "use" and "downstream got 21" in ln.message for ln in lines))
    check("the run keeps the Spark task's output to show", tasks["agg"].output == {"value": {"rows": 21}})

    # 4. booth-spark's own refusal, as Spark says it
    pid = pipeline("real-refused", [("agg", "x = 1\n", [], {"runner": "spark", "spark": {"conf": {"spark.driver.extraJavaOptions": "-Dx"}}})])
    run, _ = wait(svc.start_scheduled(svc.get_pipeline(owner, pid)).id, 300)
    report["refused"] = {"status": run.status, "error": run.error}
    check("booth-spark's refusal reaches the run as its own message", run.status == "failed" and "booth-spark refused the application (HTTP 400)" in (run.error or "") and "(invalid)" in (run.error or ""))

    # 5. cancel: pipeline's stop reaches booth-spark
    pid = pipeline("real-cancel", [("agg", SPARK_LONG, [], {"runner": "spark", "spark": small, "timeoutSeconds": 900})])
    run_id = svc.start_scheduled(svc.get_pipeline(owner, pid)).id
    deadline, ext = time.monotonic() + 600, {}
    while time.monotonic() < deadline and ext.get("state") != "running":
        time.sleep(2)
        ext = next((t.external for t in svc.get_run(owner, run_id)[1] if t.task_key == "agg"), None) or {}
    check("the long task's application reached running", ext.get("state") == "running")
    svc.cancel_run(owner, run_id)
    run, _ = wait(run_id, 300)
    token = workload(f"pipeline:acme:{run_id}")  # the run's own subject: booth-spark lets its submitter look
    state, deadline = "", time.monotonic() + 180
    while time.monotonic() < deadline and state != "stopped":
        time.sleep(3)
        st, _, a = call("GET", f"/modules/spark/v1/applications/{ext.get('id')}", token=token)
        state = a.get("state", "") if isinstance(a, dict) else ""
    report["cancel"] = {"status": run.status, "spark_state": state}
    check("a canceled run is canceled", run.status == "canceled")
    check("and real booth-spark stopped its application", state == "stopped")

    print(json.dumps(report, default=str))
    return 0 if all(checks.values()) else 1


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
    if mode == "duckdb":
        return duckdb_e2e(args[0])
    if mode == "s3":
        return s3(args[0], args[1], int(args[2]) if len(args) > 2 else 0)
    if mode == "probe":
        return probe(args)
    if mode == "egress":
        return egress(args)
    if mode == "pip-probe":
        return pip_probe(args)
    if mode == "pip-secrets":
        return pip_secrets(args[0], args[1])
    if mode == "pip-run":
        return pip_run_mode(args)
    if mode == "pip-scrape":
        return pip_scrape(args[0])
    if mode == "spark-e2e":
        return spark_e2e()
    if mode == "spark-real":
        return spark_real()
    raise SystemExit(f"unknown mode {mode!r}; expected isolation, coldstart or probe (label {TASK_JOB_LABEL})")


if __name__ == "__main__":
    sys.exit(main())
