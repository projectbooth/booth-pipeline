"""ADR 0123 against the real publishers (tests/integration/realstack-events.sh): booth-storage publishing
storage.object.created and booth-api publishing api.write.committed onto booth-core's real, authenticated
bus, and booth-pipeline's deployed consumer and scheduler firing runs. Runs as a Job in the cluster, as
a person (Keycloak password grants) and as an app (a workload token minted with a stand-in streamlit
module's credential), through core's gateway only. Prints one line per check and exits non-zero on any
failure.

What it checks:
  1. a file uploaded through booth-storage fires an event trigger; the run's task sees it in ctx.trigger;
  2. a 20-file burst makes one run (debounce); a steady trickle fires at the max wait, not never;
  3. queue_one: uploads during a run make exactly one follow-up; skip: none;
  4. the loop guard: a file the pipeline's own run writes into its watched folder (as
     pipeline:acme:<runId>, through booth-storage) doesn't fire it again;
  5. an app's write through booth-api fires an event trigger watching that API.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid

import httpx
import psycopg

CORE = os.environ.get("CORE_URL", "http://booth-core.booth-system.svc:8080")
KEYCLOAK = os.environ.get("KEYCLOAK_URL", "http://keycloak.keycloak.svc:8080")
PASSWORD = os.environ["TEST_PASSWORD"]
MINT_URL = os.environ["STREAMLIT_MINT_URL"]
MINT_CRED = os.environ["STREAMLIT_MINT_CREDENTIAL"]
WS = "acme"
http = httpx.Client(timeout=30)
results: dict[str, bool] = {}
report: dict[str, object] = {}


def check(name: str, ok: bool, detail: object = None) -> None:
    results[name] = bool(ok)
    print(("ok   " if ok else "FAIL ") + name + ("" if ok or detail is None else f"  -- {detail}"), flush=True)


def step(s: str) -> None:
    print(f"\n--- {s}", flush=True)


def login(user: str) -> str:
    r = http.post(f"{KEYCLOAK}/realms/booth/protocol/openid-connect/token",
                  data={"grant_type": "password", "client_id": "booth-design", "username": user, "password": PASSWORD, "scope": "openid"})  # fmt: skip
    r.raise_for_status()
    return r.json()["access_token"]


def sub_of(token: str) -> str:
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["sub"]


def call(method: str, path: str, token: str, body=None, expect: int | tuple = (200, 201, 202, 204), **headers):
    h = {"Authorization": f"Bearer {token}", "X-Workspace": WS, **headers}
    r = http.request(method, CORE + path, json=body, headers=h) if not isinstance(body, bytes) else http.request(method, CORE + path, content=body, headers=h)
    codes = expect if isinstance(expect, tuple) else (expect,)
    if r.status_code not in codes:
        raise SystemExit(f"{method} {path}: HTTP {r.status_code}, want {codes}: {r.text[:500]}")
    return r.json() if r.content and "json" in r.headers.get("content-type", "") else None


PIPE = "/modules/pipeline/api"


def upload(token: str, path: str, data: bytes = b"a,b\n1,2\n") -> None:
    call("PUT", f"/modules/storage/api/backends/local/objects/{path}", token, data, (200, 201, 204), **{"Content-Type": "text/csv"})


def pipeline(token: str, name: str, source: str, trigger: dict, **task_kw) -> str:
    t = call("POST", f"{PIPE}/tasks", token, {"name": f"{name}-task-{uuid.uuid4().hex[:6]}", "config": {"code": {"type": "inline", "source": source}, **task_kw}})
    p = call("POST", f"{PIPE}/pipelines", token, {"name": f"{name}-{uuid.uuid4().hex[:6]}",
             "spec": {"tasks": [{"key": "load", "taskId": t["id"], "taskVersion": 1, "dependsOn": [], "position": {"x": 0, "y": 0}}]}})  # fmt: skip
    call("PUT", f"{PIPE}/pipelines/{p['id']}/schedule", token, {"eventTrigger": {"match": trigger.pop("match"), **trigger}, "allowConcurrentRuns": False})
    return p["id"]


def runs(token: str, pid: str) -> list[dict]:
    items = call("GET", f"{PIPE}/runs?pipelineId={pid}&limit=100", token)["items"]
    return sorted(items, key=lambda r: r["createdAt"])


def wait_runs(token: str, pid: str, n: int, timeout: float, settle: float = 0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rs = runs(token, pid)
        if len(rs) >= n and all(r["status"] in ("succeeded", "failed", "canceled") for r in rs):
            break
        time.sleep(1)
    if settle:
        time.sleep(settle)
    return [call("GET", f"{PIPE}/runs/{r['id']}", token) for r in runs(token, pid)]


def wait_status(token: str, pid: str, status: str, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(r["status"] == status for r in runs(token, pid)):
            return
        time.sleep(1)
    raise SystemExit(f"no {status} run of {pid} within {timeout}s")


def logs(token: str, run_id: str) -> list[str]:
    return [ln["message"] for ln in call("GET", f"{PIPE}/runs/{run_id}/logs?limit=5000", token)["items"]]


SHOW = "def run(ctx):\n    print('trigger', ctx.trigger.type, ctx.trigger.count)\n    for e in ctx.trigger.events:\n        print('event', e.get('type'), e.get('path') or e.get('apiSlug'), e.get('principal'), e.get('via') or e.get('operation'))\n"
SLOW = "import time\ndef run(ctx):\n    print('trigger', ctx.trigger.type, ctx.trigger.count)\n    time.sleep(25)\n"
LOOP = (
    "def run(ctx):\n"
    "    print('trigger', ctx.trigger.type, ctx.trigger.count)\n"
    "    ctx.storage.write('local', 'inbox/loop/written-by-' + ctx.run_id + '.txt', b'from the run')\n"
)


def main() -> int:
    step("sign in (core records each person's roles), and an acme filesystem backend in booth-storage")
    alice = login("alice")  # an acme owner
    call("GET", "/api/me", alice)
    alice_sub = sub_of(alice)
    have = [b["id"] for b in call("GET", "/modules/storage/api/admin/backends", alice) or []]
    if "local" not in have:
        call("POST", "/modules/storage/api/admin/backends", alice, {"id": "local", "displayName": "Local", "kind": "filesystem", "config": {"rootPath": "/data/acme"}})
    check("pipeline's config says event triggers are available here (it holds core's bus credential)", call("GET", f"{PIPE}/config", alice)["eventTriggers"] == {"available": True})

    step("1. a file uploaded through booth-storage fires the trigger; the task sees it")
    p1 = pipeline(alice, "ev-file", SHOW, {"match": [{"storage": {"backendId": "local", "prefix": "inbox/one/"}}], "debounceSeconds": 2})
    upload(alice, "inbox/one/statement.csv")
    rs = wait_runs(alice, p1, 1, 180, settle=8)
    report["file"] = [(r["status"], r["triggeredBy"], r.get("triggerEvents")) for r in rs]
    check("one upload, one event-fired run", len(rs) == 1 and rs[0]["trigger"] == "event" and rs[0]["status"] == "succeeded", report["file"])
    if rs:
        ev = (rs[0].get("triggerEvents") or {}).get("events") or [{}]
        check("the event is booth-storage's: the path, the uploader's sub, via workspace-user",
              ev[0].get("path") == "inbox/one/statement.csv" and ev[0].get("principal") == alice_sub and ev[0].get("via") == "workspace-user", ev)  # fmt: skip
        check("the task saw it as ctx.trigger", any(m.startswith("event storage.object.created inbox/one/statement.csv") for m in logs(alice, rs[0]["id"])))

    step("2. a burst makes one run; a steady trickle fires at the max wait")
    p2 = pipeline(alice, "ev-burst", SHOW, {"match": [{"storage": {"backendId": "local", "prefix": "inbox/burst/"}}], "debounceSeconds": 5, "maxWaitSeconds": 120})
    for i in range(20):
        upload(alice, f"inbox/burst/{i:02}.csv")
    rs = wait_runs(alice, p2, 1, 180, settle=12)
    report["burst"] = [(r["status"], (r.get("triggerEvents") or {}).get("count")) for r in rs]
    check("20 uploads in a burst make exactly one run, told about all 20", len(rs) == 1 and (rs[0].get("triggerEvents") or {}).get("count") == 20, report["burst"])

    p3 = pipeline(alice, "ev-trickle", SHOW, {"match": [{"storage": {"backendId": "local", "prefix": "inbox/trickle/"}}], "debounceSeconds": 10, "maxWaitSeconds": 15, "minGapSeconds": 5})
    started = time.monotonic()
    for i in range(12):  # one every 3 s for 36 s: never 10 s of quiet
        upload(alice, f"inbox/trickle/{i:02}.csv")
        time.sleep(3)
    during = runs(alice, p3)
    rs = wait_runs(alice, p3, 1, 120, settle=15)
    report["trickle"] = {"runsWhileTrickling": len(during), "seconds": round(time.monotonic() - started), "counts": [(r.get("triggerEvents") or {}).get("count") for r in rs]}
    check("a trickle with no quiet still fires, at the max wait (runs started while it went on)", len(during) >= 2, report["trickle"])
    check("and every trickled file reached some run", sum((r.get("triggerEvents") or {}).get("count") or 0 for r in rs) == 12, report["trickle"])

    step("3. when a run is active: queue_one makes one follow-up, skip none")
    for mode, follow in (("queue_one", 1), ("skip", 0)):
        p = pipeline(alice, f"ev-{mode}", SLOW, {"match": [{"storage": {"backendId": "local", "prefix": f"inbox/{mode}/"}}], "debounceSeconds": 2, "whenRunning": mode})
        upload(alice, f"inbox/{mode}/first.csv")
        wait_status(alice, p, "running")
        for i in range(3):
            upload(alice, f"inbox/{mode}/during-{i}.csv")
        rs = wait_runs(alice, p, 1 + follow, 240, settle=20)
        report[mode] = [(r["status"], (r.get("triggerEvents") or {}).get("count")) for r in rs]
        check(f"{mode}: uploads during a run make {follow} follow-up run(s)", len(rs) == 1 + follow, report[mode])
        if mode == "queue_one" and len(rs) == 2:
            check("the follow-up carries the 3 uploads that arrived during the run", (rs[1].get("triggerEvents") or {}).get("count") == 3, report[mode])
        if mode == "skip":
            st = call("GET", f"{PIPE}/pipelines/{p}/event-trigger", alice)
            check("skip counted what it dropped", st["skippedWhileRunning"] == 3, st)

    step("4. the loop guard: the run's own write into its watched folder doesn't fire it again")
    p4 = pipeline(alice, "ev-loop", LOOP, {"match": [{"storage": {"backendId": "local", "prefix": "inbox/loop/"}}], "debounceSeconds": 2}, platformAccess=True)
    upload(alice, "inbox/loop/seed.csv")
    rs = wait_runs(alice, p4, 1, 180, settle=20)
    st = call("GET", f"{PIPE}/pipelines/{p4}/event-trigger", alice)
    report["loop"] = {"runs": [(r["status"], r["error"]) for r in rs], "ignoredOwnRuns": st["ignoredOwnRuns"], "lastEvent": st["lastEvent"]}
    check("the run succeeded and wrote its file through booth-storage", bool(rs) and rs[0]["status"] == "succeeded", report["loop"])
    check("its own write was seen and ignored (principal pipeline:acme:<runId>), so one run only",
          len(rs) == 1 and st["ignoredOwnRuns"] == 1, report["loop"])  # fmt: skip

    step("5. an app's write through booth-api fires the trigger watching that API")
    lease = call("POST", "/api/credentials", alice, {"kind": "postgres", "access": "readwrite", "ttlSeconds": 300, "scope": {}}, 201)["credential"]
    with psycopg.connect(host=lease["host"], port=lease["port"], dbname=lease["database"], user=lease["username"], password=lease["password"], sslmode=lease.get("sslMode") or lease.get("sslmode") or "disable") as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS events_input; CREATE TABLE IF NOT EXISTS events_input.entries (id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, amount numeric(12,2) NOT NULL, entered_by text NOT NULL)")
    dataset = None
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline and dataset is None:
        for d in call("GET", "/modules/catalog/api/datasets?limit=200", alice)["items"]:
            pt = d.get("postgresTable") or {}
            if d.get("format") == "postgres" and pt.get("schema") == "events_input" and pt.get("name") == "entries":
                dataset = d["id"]
        time.sleep(5)
    check("the table was auto-listed in the catalog", dataset is not None)
    api = call("POST", "/modules/api/api/apis", alice, {"datasetId": dataset}, 201)
    slug, api_id = api["slug"], api["id"]
    call("PUT", f"/modules/api/api/apis/{api_id}/writes", alice, {"operations": ["insert"], "columns": ["amount", "entered_by"]})
    call("POST", "/modules/api/api/write-grants", alice, {"apiId": api_id, "principal": "streamlit:acme:budget", "operations": ["insert"]}, 201)
    r = http.post(MINT_URL, headers={"Authorization": f"Bearer {MINT_CRED}"}, json={"workspace": WS, "subject": "streamlit:acme:budget", "roleCeiling": "viewer", "owner": alice_sub})
    r.raise_for_status()
    app_token = r.json()["token"]
    p5 = pipeline(alice, "ev-api", SHOW, {"match": [{"api": {"slugs": [slug]}}], "debounceSeconds": 2})
    r = http.post(f"{CORE}/modules/api/app/v1/{slug}/rows", headers={"Authorization": f"Bearer {app_token}", "X-Workspace": WS, "X-Booth-App-User": "sam@example.test"},
                  json={"data": {"amount": "12.50", "entered_by": "sam"}})  # fmt: skip
    check("the app's write was committed", r.status_code == 201, r.text[:300])
    rs = wait_runs(alice, p5, 1, 180, settle=8)
    report["api"] = [(r["status"], r.get("triggerEvents")) for r in rs]
    check("one write, one event-fired run", len(rs) == 1 and rs[0]["status"] == "succeeded", report["api"])
    if rs:
        ev = (rs[0].get("triggerEvents") or {}).get("events") or [{}]
        check("the event is booth-api's: the API, an insert, the app's principal",
              ev[0].get("apiSlug") == slug and ev[0].get("operation") == "insert" and ev[0].get("principal") == "streamlit:acme:budget", ev)  # fmt: skip
        check("the task saw it as ctx.trigger", any(m.startswith(f"event api.write.committed {slug}") for m in logs(alice, rs[0]["id"])))

    print("\n" + json.dumps(report, default=str))
    failed = [k for k, v in results.items() if not v]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; failed: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
