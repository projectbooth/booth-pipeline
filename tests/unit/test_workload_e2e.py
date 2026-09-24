"""The whole workload-identity path, over real sockets: API/scheduler app -> mint (fake core) ->
the separate runner service -> task code -> storage/catalog (fake). What it exists to prove:

* a task that opted in reaches storage/catalog with a token core minted for THIS run;
* the minting credential never reaches the runner or the run log (ADR 0057);
* a 403 from core is surfaced plainly in the run's status and log, and is not retried (ADR 0058).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from booth_pipeline.app import create_app
from booth_pipeline.catalog_client import CatalogClient
from booth_pipeline.config import Config
from booth_pipeline.runner_service import create_runner_app
from booth_pipeline.runners.subprocess_runner import SubprocessRunner
from booth_pipeline.store.memory import MemoryStore
from booth_pipeline.workload import OWNER_NOT_CURRENT, WorkloadMinter

from .harness import FakeCatalog, FakeVerifier, task
from .servers import FakeHttp, serve_asgi

CRED = "bwmc.pipeline.NEVER-LEAVE-THE-API-POD"
RUNNER_SECRET = "runner-secret"

TASK_SRC = (
    "def run(ctx):\n"
    "    ctx.storage.write('b1', 'out/result.txt', 'hello')\n"
    "    print('registered', ctx.catalog.register_dataset('ds', 'b1', 'out/result.txt')['id'])\n"
    "    return 'done'\n"
)


class RecordingRunner(SubprocessRunner):
    """The runner side, remembering everything it was ever handed."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    def run(self, inv, log, cancel):
        self.seen.append(repr(inv) + repr(inv.access))
        return super().run(inv, log, cancel)


class World:
    def __init__(self, client: TestClient, core: FakeHttp, platform: FakeHttp, runner: RecordingRunner, store: MemoryStore, app) -> None:
        self.client, self.core, self.platform, self.runner, self.store, self.app = client, core, platform, runner, store, app

    def call(self, method: str, path: str, token: str = "tok-editor", json=None):
        return self.client.request(method, "/api" + path, json=json, headers={"Authorization": f"Bearer {token}", "X-Booth-Workspace": "acme"})

    def wait_run(self, run_id: str, timeout: float = 90.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            r = self.call("GET", f"/runs/{run_id}").json()
            if r["status"] in ("succeeded", "failed", "canceled"):
                return r
            time.sleep(0.1)
        raise AssertionError("run did not finish")

    def logs(self, run_id: str) -> str:
        return "\n".join(ln["message"] for ln in self.call("GET", f"/runs/{run_id}/logs").json()["items"])

    def mints(self):
        return [r for r in self.core.requests if r.path == "/api/internal/workload-tokens"]

    def pipeline(self, spec_tasks, token="tok-editor", **schedule_kw):
        refs = [self._task(n, token) for n in spec_tasks]
        p = self.call("POST", "/pipelines", token, json={"name": f"p{len(self.store._pipelines)}", "spec": {"tasks": refs}})
        assert p.status_code == 201, p.text
        body = p.json()
        if schedule_kw:
            r = self.call("PUT", f"/pipelines/{body['id']}/schedule", token, json=schedule_kw)
            assert r.status_code == 200, r.text
            body = {**body, **r.json()}
        return body

    def _task(self, n, token):
        n = dict(n)
        key = n.pop("key")
        deps = n.pop("dependsOn", [])
        code = n.pop("code")
        r = self.call("POST", "/tasks", token, json={"name": key, "config": {"code": code, **n}})
        assert r.status_code == 201, r.text
        return {"key": key, "taskId": r.json()["id"], "taskVersion": 1, "dependsOn": deps, "position": {"x": 0, "y": 0}}


@pytest.fixture()
def world():
    core, platform = FakeHttp(), FakeHttp()
    n = {"i": 0}

    def core_handler(req):
        n["i"] += 1
        return 200, {"token": f"minted-{n['i']}", "tokenType": "Bearer", "expiresAt": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(), "role": req.json()["roleCeiling"]}

    core.handler = core_handler
    platform.handler = lambda req: (201, {"id": "d-1"}) if req.method == "POST" else (200, {"path": "out/result.txt", "size": 5})
    runner = RecordingRunner()
    minter = WorkloadMinter(CRED, core.url + "/api/internal/workload-tokens", core.url)
    with serve_asgi(create_runner_app(RUNNER_SECRET, runner)) as runner_url:
        cfg = Config(
            dev_memory=True,
            oidc_issuer_url="https://idp.test/realms/booth",
            oidc_client_id="c",
            runner_url=runner_url,
            runner_auth_token=RUNNER_SECRET,
            core_url=platform.url,  # tasks reach storage/catalog through "the gateway" at core_url/modules/...
        )
        store = MemoryStore()
        catalog = CatalogClient("http://core.test", transport=httpx.MockTransport(FakeCatalog().handler))
        app = create_app(cfg, store=store, verifier=FakeVerifier(), catalog=catalog, start_scheduler=False, minter=minter)
        with TestClient(app) as client:
            yield World(client, core, platform, runner, store, app)
    core.close()
    platform.close()


def platform_task(key="write", **kw):
    return task(key, ["src"], source=TASK_SRC, platformAccess=True, **kw)


def spec_with_platform(**kw):
    return [task("src"), platform_task(**kw)]


def test_an_opted_in_task_reaches_storage_and_the_catalog_with_a_token_minted_for_this_run(world: World):
    p = world.pipeline(spec_with_platform())
    run = world.wait_run(world.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert run["status"] == "succeeded", run
    assert "registered d-1" in world.logs(run["id"])

    (mint,) = world.mints()  # exactly one: 'src' did not opt in, so it cost nothing
    assert mint.headers["authorization"] == f"Bearer {CRED}"
    body = mint.json()
    assert body["workspace"] == "acme" and body["subject"] == f"run:{run['id']}" and body["roleCeiling"] == "editor"
    assert body["owner"] == "u-editor"  # a manual run belongs to whoever pressed the button (their `sub`, not their name)
    assert not body["subject"].startswith("u-") and "olga" not in body["subject"]  # the token names the run, never a person

    reqs = world.platform.requests
    assert {r.path.split("/api/")[0] for r in reqs} == {"/modules/storage", "/modules/catalog"}
    assert all(r.headers["authorization"] == "Bearer minted-1" and r.headers["x-workspace"] == "acme" for r in reqs)


def test_the_minting_credential_never_reaches_the_runner_or_the_run_log(world: World):
    p = world.pipeline(spec_with_platform())
    run = world.wait_run(world.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert run["status"] == "succeeded"
    assert world.runner.seen, "the runner should have received the task"
    for what in world.runner.seen:
        assert CRED not in what and "bwmc" not in what  # ADR 0057: only the short-lived token goes down
        assert "minted-1" in what or "access=None" in what
    assert CRED not in world.logs(run["id"]) and "minted-" not in world.logs(run["id"])
    # nor is the token echoed into anything a user can read through the API
    assert "minted-" not in str(world.call("GET", f"/runs/{run['id']}").json())


def test_a_task_that_never_opted_in_cannot_use_storage_and_costs_no_mint(world: World):
    src = "def run(ctx):\n    ctx.storage.backends()\n"
    p = world.pipeline([task("only", source=src)])
    run = world.wait_run(world.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert run["status"] == "failed" and world.mints() == []
    assert "no platform access" in world.logs(run["id"])


def test_the_role_ceiling_is_sent_and_it_can_be_lowered_to_viewer(world: World):
    p = world.pipeline(spec_with_platform(), schedule={"cron": "0 9 * * *"}, roleCeiling="viewer")
    assert p["roleCeiling"] == "viewer"
    world.wait_run(world.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert world.mints()[0].json()["roleCeiling"] == "viewer"


def test_owner_can_never_be_a_ceiling(world: World):
    p = world.pipeline([task("a")])
    r = world.call("PUT", f"/pipelines/{p['id']}/schedule", json={"schedule": {"cron": "0 9 * * *"}, "roleCeiling": "owner"})
    assert r.status_code == 422  # nothing a pipeline does needs workspace administration


def test_a_scheduled_run_is_owned_by_whoever_first_gave_the_pipeline_a_schedule(world: World):
    p = world.pipeline(spec_with_platform(), token="tok-owner", schedule={"cron": "0 * * * *"})  # scheduled by olga (u-owner)
    # an editor edits the schedule; ownership must not move (a moved owner would launder identity)
    world.call("PUT", f"/pipelines/{p['id']}/schedule", "tok-editor", json={"schedule": {"cron": "0 * * * *"}})
    stored = world.store.get_pipeline("acme", p["id"])
    assert stored.owner_sub == "u-owner"
    stored.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
    world.store.update_schedule(stored)
    (rid,) = world.app.state.scheduler.tick()
    run = world.wait_run(rid)
    assert run["status"] == "succeeded" and run["trigger"] == "schedule"
    assert world.mints()[0].json()["owner"] == "u-owner"


# ---- 403: the owner has not signed in recently enough (ADR 0058) -----------------------------


def refuse(world: World):
    world.core.handler = lambda req: (403, {"error": "forbidden"})


def test_a_lapsed_owner_fails_the_run_with_a_clear_reason_in_status_and_log_and_is_not_retried(world: World):
    refuse(world)
    p = world.pipeline(spec_with_platform(retry={"maxRetries": 3}))
    run = world.wait_run(world.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert run["status"] == "failed"
    # in the run's OWN status, where a person looks first...
    assert "signed in" in run["error"] and "recently enough" in run["error"] and "write:" in run["error"]
    task_run = {t["taskKey"]: t for t in run["tasks"]}
    assert task_run["write"]["status"] == "failed" and task_run["write"]["attempts"] == 1  # never retried
    assert task_run["src"]["status"] == "succeeded"  # the task that needed nothing was unaffected
    # ...and in the log, with the explanation
    logs = world.logs(run["id"])
    assert OWNER_NOT_CURRENT in logs and "not given platform access" in logs
    assert len(world.mints()) == 1  # three retries configured, one mint attempt
    assert world.platform.requests == []  # the task's code never ran, so it never called storage


def test_a_pipeline_that_needs_no_platform_access_is_unaffected_by_a_lapsed_owner(world: World):
    refuse(world)
    p = world.pipeline([task("a"), task("b", ["a"])])
    assert world.wait_run(world.call("POST", f"/pipelines/{p['id']}/run").json()["id"])["status"] == "succeeded"
    assert world.mints() == []


def test_a_pipeline_that_predates_workload_identity_says_how_to_fix_it(world: World):
    p = world.pipeline(spec_with_platform())
    stored = world.store.get_pipeline("acme", p["id"])
    stored.owner_sub = ""  # as created before this feature existed
    world.store.update_schedule(stored)
    run_id = world.app.state.service.start_scheduled(world.store.get_pipeline("acme", p["id"]))
    run = world.wait_run(run_id.id)
    assert run["status"] == "failed" and "no recorded owner" in run["error"] and "save its schedule again" in run["error"]
    # saving a schedule (by anyone active) adopts it
    world.call("PUT", f"/pipelines/{p['id']}/schedule", "tok-owner", json={"schedule": {"cron": "0 9 * * *"}})
    assert world.store.get_pipeline("acme", p["id"]).owner_sub == "u-owner"


def test_the_startup_guard_refuses_a_minting_credential_in_a_pod_that_runs_tasks(tmp_path):
    """ADR 0057: with no runner URL, tasks run in THIS pod and could read the mounted Secret."""
    (tmp_path / "credential").write_text(CRED)
    from booth_pipeline.config import ConfigError

    base = dict(dev_memory=True, oidc_issuer_url="https://i/r", oidc_client_id="c", workload_mint_dir=str(tmp_path))
    with pytest.raises(ConfigError, match="same pod and could read it"):
        Config(**base).validate()
    Config(**base, runner_url="http://runner:8080", runner_auth_token="s").validate()  # fine once tasks run elsewhere
    Config(**{**base, "workload_mint_dir": str(tmp_path / "absent")}).validate()  # fine when there is no credential at all
