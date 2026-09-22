"""The real app against a real PostgreSQL and a real Keycloak — nothing faked but the catalog.

Everything the unit tests fake is real here: tokens are RS256-signed by Keycloak and verified
through its published JWKS, ``groups`` claims come from Keycloak's group-membership mapper, and
every row goes through psycopg into PostgreSQL. It exists because the unit tests prove our logic
against *our model of* those systems, and only this proves the model right.

Skipped unless both are available (see hack/docker-compose.test.yml and hack/dev-keycloak.sh):

    docker compose -f hack/docker-compose.test.yml up -d --wait
    hack/dev-keycloak.sh up
    BOOTH_TEST_POSTGRES_DSN=... BOOTH_TEST_KEYCLOAK_URL=http://localhost:8081 pytest tests/integration

This is NOT the real-cluster layer (testing-strategy.md layer 3: kind/k3d beside a real booth-core);
that is .github/workflows/integration.yml.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from booth_pipeline.app import create_app
from booth_pipeline.config import Config
from booth_pipeline.store.postgres import PostgresStore

DSN = os.environ.get("BOOTH_TEST_POSTGRES_DSN")
KC = os.environ.get("BOOTH_TEST_KEYCLOAK_URL", "").rstrip("/")
REALM = "booth-local"
pytestmark = pytest.mark.skipif(not (DSN and KC), reason="needs BOOTH_TEST_POSTGRES_DSN and BOOTH_TEST_KEYCLOAK_URL")

ISSUER = f"{KC}/realms/{REALM}"


def token_for(user: str) -> str:
    r = httpx.post(
        f"{ISSUER}/protocol/openid-connect/token",
        data={"grant_type": "password", "client_id": "booth-design", "username": user, "password": "booth-dev-password"},
        timeout=20,
    )
    assert r.status_code == 200, f"could not mint a token for {user}: {r.text} (did you run hack/dev-keycloak.sh up?)"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def tokens():
    return {u: token_for(u) for u in ("alice", "bob", "carol", "dave")}  # owner, editor, viewer, no membership


@pytest.fixture()
def clean_db():
    store = PostgresStore(DSN)
    store.migrate()
    with store._pool.connection() as conn:
        conn.execute("TRUNCATE run_logs, task_runs, runs, jobs, pipeline_versions, pipelines")
    store.close()


def make_client(**over) -> TestClient:
    cfg = Config(database_dsn=DSN, oidc_issuer_url=ISSUER, oidc_require_audience=False, **over)
    return TestClient(create_app(cfg, start_scheduler=False))


def call(c: TestClient, tok: str, method: str, path: str, json=None, headers=None, ws="acme"):
    return c.request(method, "/api" + path, json=json, headers={"Authorization": f"Bearer {tok}", "X-Booth-Workspace": ws, **(headers or {})})


def wait_run(c, tok, run_id, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = call(c, tok, "GET", f"/runs/{run_id}").json()
        if r["status"] in ("succeeded", "failed", "canceled"):
            return r
        time.sleep(0.2)
    raise AssertionError(f"run did not finish: {r}")


SPEC = {
    "tasks": [
        {"key": "extract", "kind": "source", "code": {"type": "inline", "source": "def run(ctx):\n    print('rows: 3')\n    return [1, 2, 3]\n"}},
        {"key": "load", "kind": "sink", "dependsOn": ["extract"], "code": {"type": "inline", "source": "def run(ctx):\n    print('sum', sum(ctx.inputs['extract']))\n"}},
    ]
}


def test_real_tokens_real_roles_real_database(clean_db, tokens):
    with make_client() as c:
        # a real Keycloak-issued token is accepted; roles come from its real groups claim
        assert call(c, tokens["carol"], "GET", "/pipelines").status_code == 200  # viewer reads
        assert call(c, tokens["carol"], "POST", "/pipelines", {"name": "x"}).status_code == 403  # ...but cannot write
        assert call(c, tokens["dave"], "GET", "/pipelines").status_code == 403  # authenticated, no membership
        assert call(c, tokens["bob"], "GET", "/pipelines", ws="some-other-workspace").status_code == 403  # member of acme only
        assert c.get("/api/pipelines", headers={"X-Booth-Workspace": "acme"}).status_code == 401
        assert call(c, tokens["bob"][:-6] + "AAAAAA", "GET", "/pipelines").status_code == 401  # tampered signature

        # ADR 0041, measured against a real IdP: a genuine viewer token + a forged owner header
        forged = call(c, tokens["carol"], "POST", "/pipelines", {"name": "x"}, headers={"X-Booth-Role": "owner"})
        assert forged.status_code == 403 and "exceeds" in forged.json()["error"]

        # the whole loop as an editor: pipeline -> job -> run -> per-task state + logs
        p = call(c, tokens["bob"], "POST", "/pipelines", {"name": "etl", "spec": SPEC}).json()
        job = call(c, tokens["bob"], "POST", "/jobs", {"name": "nightly", "pipelineId": p["id"], "schedule": {"cron": "0 2 * * *", "timezone": "America/Toronto"}}).json()
        run = wait_run(c, tokens["alice"], call(c, tokens["bob"], "POST", f"/jobs/{job['id']}/run").json()["id"])
        assert run["status"] == "succeeded" and run["triggeredBy"] == "bob"
        assert {t["taskKey"]: t["status"] for t in run["tasks"]} == {"extract": "succeeded", "load": "succeeded"}
        logs = call(c, tokens["carol"], "GET", f"/runs/{run['id']}/logs").json()  # a viewer can read logs
        assert logs["done"] and [ln["message"] for ln in logs["items"] if ln["stream"] == "stdout"] == ["rows: 3", "sum 6"]
        run_id, job_id, pipeline_id = run["id"], job["id"], p["id"]

    # a brand-new app process on the same database still has everything: it is really persisted
    with make_client() as c2:
        assert call(c2, tokens["carol"], "GET", f"/pipelines/{pipeline_id}/versions/1").json()["taskCount"] == 2
        assert call(c2, tokens["carol"], "GET", f"/jobs/{job_id}").json()["schedule"]["timezone"] == "America/Toronto"
        again = call(c2, tokens["carol"], "GET", f"/runs/{run_id}").json()
        assert again["status"] == "succeeded" and len(call(c2, tokens["carol"], "GET", f"/runs/{run_id}/logs").json()["items"]) >= 6


def test_two_app_processes_never_double_start_a_scheduled_fire(clean_db, tokens):
    """The multi-replica claim, on real PostgreSQL row locks, through two independent apps."""
    from datetime import UTC, datetime, timedelta

    with make_client() as a, make_client() as b:
        p = call(a, tokens["bob"], "POST", "/pipelines", {"name": "etl", "spec": SPEC}).json()
        job = call(a, tokens["bob"], "POST", "/jobs", {"name": "j", "pipelineId": p["id"], "schedule": {"cron": "*/5 * * * *"}}).json()
        store = a.app.state.store
        j = store.get_job("acme", job["id"])
        j.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
        store.update_job(j)
        import threading

        out: list[list[str]] = []
        barrier = threading.Barrier(2)

        def poll(cli):
            barrier.wait()
            out.append(cli.app.state.scheduler.tick())

        ts = [threading.Thread(target=poll, args=(x,)) for x in (a, b)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        started = [r for o in out for r in o]
        assert len(started) == 1
        assert wait_run(a, tokens["bob"], started[0])["status"] == "succeeded"


def test_the_image_runs_hardened_against_the_real_database(clean_db):
    """The built image, run exactly as the chart runs it: non-root, read-only root filesystem,
    /tmp as the only writable path. Skipped when the image has not been built."""
    if subprocess.run(["docker", "image", "inspect", "booth-pipeline:dev"], capture_output=True).returncode != 0:
        pytest.skip("build the image first: docker build -t booth-pipeline:dev .")
    dsn = DSN.replace("127.0.0.1", "host.docker.internal").replace("localhost", "host.docker.internal")
    name = f"bp-smoke-{uuid.uuid4().hex[:6]}"
    port = 18089
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "--read-only", "--tmpfs", "/tmp", "--user", "65532:65532", "--cap-drop", "ALL",
         "--security-opt", "no-new-privileges", "-p", f"{port}:8080", "-e", f"BOOTH_PIPELINE_DATABASE_DSN={dsn}",
         "-e", f"BOOTH_OIDC_ISSUER_URL={ISSUER.replace('localhost', 'host.docker.internal')}", "-e", "BOOTH_OIDC_REQUIRE_AUDIENCE=false", "booth-pipeline:dev"],
        capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stderr
    try:
        end, last = time.monotonic() + 60, ""
        while time.monotonic() < end:
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2)
                if r.status_code == 200:
                    assert r.json()["status"] == "ok"
                    return
                last = r.text
            except httpx.HTTPError as e:
                last = str(e)
            time.sleep(1)
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True).stdout
        pytest.fail(f"hardened container never became healthy: {last}\n{logs[-2000:]}")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


