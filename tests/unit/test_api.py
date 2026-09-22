"""The HTTP API end to end: auth, pipelines, jobs and runs — real subprocess execution, in-memory
store, faked identity provider and catalog."""

from __future__ import annotations

import pytest

from .harness import Env, etl_spec, make_env, task


@pytest.fixture()
def env():
    e = make_env()
    with e.client:
        yield e


def new_pipeline(env: Env, name: str = "etl", spec=None, **kw):
    r = env.call("POST", "/pipelines", json={"name": name, "spec": spec if spec is not None else etl_spec(), **kw})
    assert r.status_code == 201, r.text
    return r.json()


def new_job(env: Env, pipeline_id: str, name: str = "nightly", **kw):
    r = env.call("POST", "/jobs", json={"name": name, "pipelineId": pipeline_id, **kw})
    assert r.status_code == 201, r.text
    return r.json()


# ---- auth (ADR 0025 / 0041) -----------------------------------------------------------------


def test_missing_and_bad_tokens_are_401(env):
    assert env.client.get("/api/pipelines", headers={"X-Booth-Workspace": "acme"}).status_code == 401
    assert env.call("GET", "/pipelines", token="tok-bogus").status_code == 401
    r = env.client.get("/api/pipelines", headers={"Authorization": "Basic abc", "X-Booth-Workspace": "acme"})
    assert r.status_code == 401 and r.json() == {"error": "missing bearer token"}


def test_missing_workspace_header_is_400_and_no_role_is_403(env):
    assert env.client.get("/api/pipelines", headers={"Authorization": "Bearer tok-editor"}).status_code == 400
    assert env.call("GET", "/pipelines", token="tok-nowhere").status_code == 403
    assert env.call("GET", "/pipelines", token="tok-other-ws").status_code == 403  # a member of ANOTHER workspace


def test_a_forged_role_header_is_rejected_not_downgraded(env):
    # a valid viewer token with an X-Booth-Role: owner header: exactly the ADR 0041 attack
    r = env.call("POST", "/pipelines", "tok-viewer", json={"name": "x"}, headers={"X-Booth-Role": "owner"})
    assert r.status_code == 403 and "exceeds" in r.json()["error"]
    assert env.call("GET", "/pipelines", "tok-editor", headers={"X-Booth-Role": "owner"}).status_code == 403


def test_the_forwarded_role_can_narrow_but_never_widen(env):
    # a gateway may legitimately restrict an editor to read-only for a request
    assert env.call("GET", "/pipelines", "tok-editor", headers={"X-Booth-Role": "viewer"}).status_code == 200
    assert env.call("POST", "/pipelines", "tok-editor", json={"name": "x"}, headers={"X-Booth-Role": "viewer"}).status_code == 403
    assert env.call("GET", "/pipelines", "tok-editor", headers={"X-Booth-Role": "superuser"}).status_code == 403


def test_viewers_read_but_cannot_write_or_run(env):
    p = new_pipeline(env)
    job = new_job(env, p["id"])
    assert env.call("GET", "/pipelines", "tok-viewer").status_code == 200
    assert env.call("GET", f"/pipelines/{p['id']}/versions/latest", "tok-viewer").status_code == 200
    for method, path, body in [
        ("POST", "/pipelines", {"name": "n"}),
        ("PUT", f"/pipelines/{p['id']}", {"name": "n"}),
        ("DELETE", f"/pipelines/{p['id']}", None),
        ("POST", f"/pipelines/{p['id']}/versions", {"spec": etl_spec()}),
        ("POST", "/jobs", {"name": "j", "pipelineId": p["id"]}),
        ("DELETE", f"/jobs/{job['id']}", None),
        ("POST", f"/jobs/{job['id']}/run", None),
    ]:
        assert env.call(method, path, "tok-viewer", json=body).status_code == 403, (method, path)


def test_another_workspace_cannot_see_or_touch_anything(env):
    env.store  # noqa: B018 - the store is shared; isolation is by workspace
    p = new_pipeline(env)
    job = new_job(env, p["id"])
    run_id = env.call("POST", f"/jobs/{job['id']}/run").json()["id"]
    from booth_pipeline.auth import Claims

    from .harness import TOKENS

    TOKENS["tok-globex"] = Claims("u-g", "gail", ("/workspaces/globex/editor",))
    try:
        for path in (f"/pipelines/{p['id']}", f"/jobs/{job['id']}", f"/runs/{run_id}", f"/runs/{run_id}/logs"):
            assert env.call("GET", path, "tok-globex", ws="globex").status_code == 404, path
        assert env.call("GET", "/pipelines", "tok-globex", ws="globex").json()["total"] == 0
        assert env.call("POST", f"/jobs/{job['id']}/run", "tok-globex", ws="globex").status_code == 404
        assert env.call("POST", f"/runs/{run_id}/cancel", "tok-globex", ws="globex").status_code == 404
    finally:
        del TOKENS["tok-globex"]
    env.wait_run(run_id)


def test_health_needs_no_auth_and_reports_the_database(env):
    assert env.client.get("/healthz").json()["status"] == "ok"
    assert env.client.get("/livez").status_code == 200
    env.store.ping = lambda: (_ for _ in ()).throw(RuntimeError("db down"))  # type: ignore[method-assign]
    r = env.client.get("/healthz")
    assert r.status_code == 503 and "unreachable" in r.json()["error"]


def test_runners_lists_base_available_and_spark_unavailable_with_a_reason(env):
    items = {r["id"]: r for r in env.call("GET", "/runners").json()["items"]}
    assert items["base"]["available"] is True
    assert items["spark"]["available"] is False and "booth-spark" in items["spark"]["reason"]


# ---- pipelines -----------------------------------------------------------------------------


def test_pipeline_versions_lifecycle(env):
    p = new_pipeline(env)
    assert p["latestVersion"] == 1 and p["version"]["version"] == 1 and p["version"]["taskCount"] == 3
    spec2 = etl_spec()
    spec2["tasks"].append(task("audit", "sink", ["clean"]))
    r = env.call("POST", f"/pipelines/{p['id']}/versions", json={"spec": spec2, "notes": "add audit"})
    assert r.status_code == 201 and r.json()["version"] == 2
    latest = env.call("GET", f"/pipelines/{p['id']}/versions/latest").json()
    assert latest["version"] == 2 and len(latest["spec"]["tasks"]) == 4 and latest["notes"] == "add audit"
    v1 = env.call("GET", f"/pipelines/{p['id']}/versions/1").json()
    assert len(v1["spec"]["tasks"]) == 3  # old versions are immutable
    assert [v["version"] for v in env.call("GET", f"/pipelines/{p['id']}/versions").json()["items"]] == [2, 1]
    assert env.call("GET", f"/pipelines/{p['id']}/versions/9").status_code == 404
    assert env.call("GET", f"/pipelines/{p['id']}/versions/abc").status_code == 404
    assert env.call("GET", f"/pipelines/{p['id']}").json()["latestVersion"] == 2


def test_empty_pipeline_can_be_created_then_drawn(env):
    r = env.call("POST", "/pipelines", json={"name": "blank"})
    p = r.json()
    assert r.status_code == 201 and p["latestVersion"] == 0 and p["version"] is None
    assert env.call("GET", f"/pipelines/{p['id']}/versions/latest").status_code == 404
    assert env.call("POST", f"/pipelines/{p['id']}/versions", json={"spec": etl_spec()}).status_code == 201


def test_duplicate_name_is_409_with_the_field(env):
    new_pipeline(env)
    r = env.call("POST", "/pipelines", json={"name": "etl"})
    assert r.status_code == 409 and r.json()["field"] == "name"


def test_a_bad_first_version_does_not_leave_an_empty_pipeline_behind(env):
    bad = {"tasks": [task("a", "source"), task("b", "sink", ["nope"])]}
    assert env.call("POST", "/pipelines", json={"name": "oops", "spec": bad}).status_code == 422
    assert env.call("GET", "/pipelines").json()["total"] == 0


@pytest.mark.parametrize(
    "spec, field, fragment",
    [
        ({"tasks": []}, "tasks", "at least one task"),
        ({"tasks": [task("a", "source"), task("b", "transform", ["zzz"])]}, "tasks[1].dependsOn", "not a task"),
        ({"tasks": [task("a", "transform", ["b"]), task("b", "transform", ["a"])]}, "tasks", "cycle"),
        ({"tasks": [task("a", "source", runner="spark")]}, "tasks[0].runner", "not available"),
        ({"tasks": [task("Bad Key", "source")]}, "spec.tasks[0].key", "lowercase"),
        ({"tasks": [task("a", "source", retry={"maxRetrys": 1})]}, "spec.tasks[0].retry.maxRetrys", "Extra inputs"),
        ({"tasks": [task("a", "source", retry={"maxRetries": 99})]}, "spec.tasks[0].retry.maxRetries", "less than or equal"),
    ],
)
def test_invalid_specs_are_422_naming_the_field(env, spec, field, fragment):
    p = new_pipeline(env, "host")
    r = env.call("POST", f"/pipelines/{p['id']}/versions", json={"spec": spec})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["field"] == field and fragment in body["error"]


def test_live_validation_endpoint_reports_without_saving(env):
    ok = env.call("POST", "/pipelines/validate", json={"spec": etl_spec()}).json()
    assert ok == {"valid": True}
    bad = env.call("POST", "/pipelines/validate", json={"spec": {"tasks": [task("a", "sink", ["ghost"])]}})
    assert bad.status_code == 200 and bad.json()["valid"] is False and bad.json()["field"] == "tasks[0].dependsOn"
    assert env.call("GET", "/pipelines").json()["total"] == 0


# ---- catalog code references (ADR 0010) ----------------------------------------------------


def catalog_task(key="a", entry="e1", version="latest", kind="source", deps=None, **kw):
    return {"key": key, "kind": kind, "code": {"type": "catalog", "entryId": entry, "version": version}, "dependsOn": deps or [], **kw}


def test_catalog_latest_is_resolved_and_snapshotted_at_save(env):
    env.catalog.add("e1", "loader", {"1.0.0": "def run(ctx):\n    return 'v1'\n", "2.0.0": "def run(ctx):\n    return 'v2'\n"})
    p = new_pipeline(env, spec={"tasks": [catalog_task()]})
    code = env.call("GET", f"/pipelines/{p['id']}/versions/1").json()["spec"]["tasks"][0]["code"]
    import hashlib

    assert code["version"] == "2.0.0"  # "latest" pinned to the concrete label
    assert code["name"] == "loader" and code["entryId"] == "e1"
    assert code["source"] == "def run(ctx):\n    return 'v2'\n"
    assert code["sha256"] == hashlib.sha256(code["source"].encode()).hexdigest()


def test_runs_use_the_snapshot_and_never_call_the_catalog(env):
    env.catalog.add("e1", "loader", {"1.0.0": "print('from catalog v1')\n"})
    p = new_pipeline(env, spec={"tasks": [catalog_task(version="1.0.0")]})
    job = new_job(env, p["id"])
    env.catalog.requests.clear()
    env.catalog.down = True  # the catalog disappears entirely after save
    env.catalog.entries["e1"]["versions"]["1.0.0"] = "print('TAMPERED')\n"  # and even if it did change
    run = env.wait_run(env.call("POST", f"/jobs/{job['id']}/run").json()["id"])
    assert run["status"] == "succeeded"
    logs = env.call("GET", f"/runs/{run['id']}/logs?task=a").json()["items"]
    assert [ln["message"] for ln in logs if ln["stream"] == "stdout"] == ["from catalog v1"]
    assert env.catalog.requests == []  # a run never touches the catalog


def test_catalog_call_carries_the_callers_own_token_and_workspace(env):
    env.catalog.add("e1", "loader", {"1.0.0": "pass\n"})
    new_pipeline(env, spec={"tasks": [catalog_task(version="1.0.0")]})
    assert env.catalog.requests
    for req in env.catalog.requests:
        assert req.headers["authorization"] == "Bearer tok-editor" and req.headers["x-workspace"] == "acme"
        assert req.url.path.startswith("/modules/catalog/api/code/e1")


def test_a_client_supplied_snapshot_is_never_trusted(env):
    env.catalog.add("e1", "loader", {"1.0.0": "print('real')\n"})
    forged = catalog_task(version="1.0.0")
    forged["code"].update({"source": "print('FORGED')\n", "sha256": "0" * 64, "name": "trust me"})
    p = new_pipeline(env, spec={"tasks": [forged]})
    code = env.call("GET", f"/pipelines/{p['id']}/versions/1").json()["spec"]["tasks"][0]["code"]
    assert code["source"] == "print('real')\n" and code["name"] == "loader"


def test_an_unrelated_edit_can_be_saved_while_the_catalog_is_down(env):
    env.catalog.add("e1", "loader", {"1.0.0": "pass\n"})
    p = new_pipeline(env, spec={"tasks": [catalog_task(version="1.0.0")]})
    env.catalog.down = True
    spec = env.call("GET", f"/pipelines/{p['id']}/versions/latest").json()["spec"]
    spec["tasks"].append(task("extra", "sink", ["a"]))  # only the previously-resolved ref is re-sent
    r = env.call("POST", f"/pipelines/{p['id']}/versions", json={"spec": spec})
    assert r.status_code == 201, r.text  # reused OUR stored snapshot; needed no catalog call
    # but a *new* catalog reference cannot be resolved while it is down:
    spec["tasks"].append(catalog_task("b", entry="e2", version="1.0.0", kind="source"))
    assert env.call("POST", f"/pipelines/{p['id']}/versions", json={"spec": spec}).status_code == 503


def test_catalog_failure_modes_map_to_useful_statuses(env):
    p = new_pipeline(env, "host")
    save = lambda spec: env.call("POST", f"/pipelines/{p['id']}/versions", json={"spec": spec})  # noqa: E731
    r = save({"tasks": [catalog_task(entry="nope")]})
    assert r.status_code == 422 and r.json()["field"] == "tasks[0].code" and "not found" in r.json()["error"]
    env.catalog.add("e1", "loader", {"1.0.0": "pass\n"})
    assert save({"tasks": [catalog_task(version="9.9.9")]}).status_code == 422
    env.catalog.not_installed = True  # the gateway's plain-text 404 is NOT "entry not found"
    r = save({"tasks": [catalog_task(version="1.0.0")]})
    assert r.status_code == 503 and "not installed" in r.json()["error"] and "Inline code needs no catalog" in r.json()["error"]
    env.catalog.not_installed, env.catalog.down = False, True
    assert save({"tasks": [catalog_task(version="1.0.0")]}).status_code == 503


def test_non_python_catalog_code_is_refused_for_the_base_runner(env):
    env.catalog.add("q", "report", {"1": "select 1"}, language="sql")
    p = new_pipeline(env, "host")
    r = env.call("POST", f"/pipelines/{p['id']}/versions", json={"spec": {"tasks": [catalog_task(entry="q", version="1")]}})
    assert r.status_code == 422 and "base runner runs Python" in r.json()["error"]


def test_inline_code_works_with_the_catalog_entirely_absent(env):
    """The brief's hard requirement: the base runner needs zero other modules."""
    env.catalog.not_installed = True
    p = new_pipeline(env)
    job = new_job(env, p["id"])
    assert env.wait_run(env.call("POST", f"/jobs/{job['id']}/run").json()["id"])["status"] == "succeeded"
    assert env.catalog.requests == []


# ---- jobs & runs ---------------------------------------------------------------------------


def test_jobs_need_a_saved_version_and_validate_their_schedule(env):
    blank = env.call("POST", "/pipelines", json={"name": "blank"}).json()
    r = env.call("POST", "/jobs", json={"name": "j", "pipelineId": blank["id"]})
    assert r.status_code == 422 and r.json()["field"] == "pipelineVersion"
    p = new_pipeline(env)
    assert env.call("POST", "/jobs", json={"name": "j", "pipelineId": p["id"], "pipelineVersion": 5}).status_code == 422
    assert env.call("POST", "/jobs", json={"name": "j", "pipelineId": "nope"}).status_code == 404
    bad = env.call("POST", "/jobs", json={"name": "j", "pipelineId": p["id"], "schedule": {"cron": "every day"}})
    assert bad.status_code == 422 and bad.json()["field"] == "schedule"
    bad = env.call("POST", "/jobs", json={"name": "j", "pipelineId": p["id"], "schedule": {"cron": "0 9 * * *", "timezone": "Mars/Base"}})
    assert bad.status_code == 422


def test_job_config_is_persisted_and_re_editable(env):
    p = new_pipeline(env)
    job = new_job(env, p["id"], schedule={"cron": "0 9 * * 1-5", "timezone": "America/Toronto"}, retry={"maxRetries": 2, "delaySeconds": 5, "backoff": "exponential"}, pipelineVersion=1)
    assert job["nextRunAt"] and job["schedule"] == {"cron": "0 9 * * 1-5", "timezone": "America/Toronto", "enabled": True}
    got = env.call("GET", f"/jobs/{job['id']}").json()
    assert got["retry"] == {"maxRetries": 2, "delaySeconds": 5.0, "backoff": "exponential"} and got["pipelineVersion"] == 1
    r = env.call("PUT", f"/jobs/{job['id']}", json={"name": "renamed", "pipelineId": p["id"], "schedule": {"cron": "*/5 * * * *", "enabled": False}, "allowConcurrentRuns": True})
    upd = r.json()
    assert upd["name"] == "renamed" and upd["schedule"]["enabled"] is False and upd["nextRunAt"] is None  # disabled: no next fire
    assert upd["retry"] is None and upd["pipelineVersion"] is None and upd["allowConcurrentRuns"] is True
    assert env.call("GET", "/jobs?q=renam").json()["total"] == 1


def test_run_now_executes_the_dag_and_records_per_task_state(env):
    p = new_pipeline(env)
    job = new_job(env, p["id"])
    r = env.call("POST", f"/jobs/{job['id']}/run")
    assert r.status_code == 202 and r.json()["trigger"] == "manual" and r.json()["triggeredBy"] == "eddie"
    run = env.wait_run(r.json()["id"])
    assert run["status"] == "succeeded" and run["error"] is None and run["pipelineVersion"] == 1
    assert run["startedAt"] and run["finishedAt"]
    tasks = {t["taskKey"]: t for t in run["tasks"]}
    assert {k: t["status"] for k, t in tasks.items()} == {"extract": "succeeded", "clean": "succeeded", "load": "succeeded"}
    assert all(t["attempts"] == 1 and t["startedAt"] and t["finishedAt"] for t in tasks.values())
    assert env.call("GET", f"/runs?jobId={job['id']}").json()["total"] == 1


def test_run_logs_per_run_per_task_with_a_polling_cursor(env):
    p = new_pipeline(env)
    job = new_job(env, p["id"])
    run = env.wait_run(env.call("POST", f"/jobs/{job['id']}/run").json()["id"])
    whole = env.call("GET", f"/runs/{run['id']}/logs").json()
    assert whole["done"] is True
    msgs = [(ln["taskKey"], ln["stream"], ln["message"]) for ln in whole["items"]]
    assert ("extract", "stdout", "extracting") in msgs and ("load", "stdout", "loading [10, 20, 30]") in msgs
    assert any(k is None and s == "system" and "run succeeded" in m for k, s, m in msgs)  # run-level lines
    seqs = [ln["seq"] for ln in whole["items"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    only = env.call("GET", f"/runs/{run['id']}/logs?task=load").json()["items"]
    assert {ln["taskKey"] for ln in only} == {"load"} and any(ln["message"] == "loading [10, 20, 30]" for ln in only)
    tail = env.call("GET", f"/runs/{run['id']}/logs?after={seqs[-3]}").json()["items"]
    assert [ln["seq"] for ln in tail] == seqs[-2:]  # the cursor returns only what is new
    paged = env.call("GET", f"/runs/{run['id']}/logs?limit=2").json()
    assert len(paged["items"]) == 2 and paged["done"] is False  # more to fetch: a poller must not stop yet


def test_failure_marks_downstream_skipped_and_explains(env):
    spec = {"tasks": [task("a", "source"), task("boom", "transform", ["a"], source="raise ValueError('bad row 7')\n"), task("z", "sink", ["boom"])]}
    p = new_pipeline(env, spec=spec)
    job = new_job(env, p["id"])
    run = env.wait_run(env.call("POST", f"/jobs/{job['id']}/run").json()["id"])
    assert run["status"] == "failed" and "boom" in run["error"]
    assert {t["taskKey"]: t["status"] for t in run["tasks"]} == {"a": "succeeded", "boom": "failed", "z": "skipped"}
    assert any("ValueError: bad row 7" in ln["message"] for ln in env.call("GET", f"/runs/{run['id']}/logs?task=boom").json()["items"])


def test_task_retry_override_and_job_default_retry(env):
    flaky = "def run(ctx):\n    if ctx.attempt < 3:\n        raise RuntimeError('flaky attempt %d' % ctx.attempt)\n    return 'ok'\n"
    spec = {"tasks": [task("a", "source", source=flaky, retry={"maxRetries": 2}), task("b", "sink", ["a"])]}
    p = new_pipeline(env, spec=spec)
    run = env.wait_run(env.call("POST", f"/jobs/{new_job(env, p['id'])['id']}/run").json()["id"])
    assert run["status"] == "succeeded"
    assert {t["taskKey"]: (t["status"], t["attempts"]) for t in run["tasks"]}["a"] == ("succeeded", 3)
    # job-level default applies to a task that has no override of its own
    spec2 = {"tasks": [task("a", "source", source=flaky)]}
    p2 = new_pipeline(env, "p2", spec=spec2)
    j2 = new_job(env, p2["id"], "j2", retry={"maxRetries": 2})
    run2 = env.wait_run(env.call("POST", f"/jobs/{j2['id']}/run").json()["id"])
    assert run2["status"] == "succeeded" and run2["tasks"][0]["attempts"] == 3
    # and with no retry configured anywhere the same code simply fails
    j3 = new_job(env, p2["id"], "j3")
    assert env.wait_run(env.call("POST", f"/jobs/{j3['id']}/run").json()["id"])["status"] == "failed"


def test_a_non_concurrent_job_refuses_a_second_active_run_and_a_concurrent_one_allows_it(env):
    slow = {"tasks": [task("a", "source", source="import time\ntime.sleep(3)\n")]}
    p = new_pipeline(env, spec=slow)
    job = new_job(env, p["id"])
    first = env.call("POST", f"/jobs/{job['id']}/run")
    assert first.status_code == 202
    second = env.call("POST", f"/jobs/{job['id']}/run")
    assert second.status_code == 409 and "active run" in second.json()["error"]
    env.wait_run(first.json()["id"])
    assert env.call("POST", f"/jobs/{job['id']}/run").status_code == 202  # free again
    conc = new_job(env, p["id"], "conc", allowConcurrentRuns=True)
    ids = [env.call("POST", f"/jobs/{conc['id']}/run").json()["id"] for _ in range(2)]
    for i in ids:
        env.wait_run(i)


def test_a_running_run_can_be_canceled(env):
    slow = {"tasks": [task("a", "source", source="import time\nprint('working', flush=True)\ntime.sleep(60)\n"), task("b", "sink", ["a"])]}
    p = new_pipeline(env, spec=slow)
    job = new_job(env, p["id"])
    run_id = env.call("POST", f"/jobs/{job['id']}/run").json()["id"]
    import time

    for _ in range(100):  # wait for it to actually be running
        if env.call("GET", f"/runs/{run_id}").json()["status"] == "running":
            break
        time.sleep(0.1)
    t0 = time.monotonic()
    assert env.call("POST", f"/runs/{run_id}/cancel").status_code == 202
    run = env.wait_run(run_id, timeout=20)
    assert run["status"] == "canceled" and time.monotonic() - t0 < 15
    assert {t["taskKey"]: t["status"] for t in run["tasks"]} == {"a": "canceled", "b": "canceled"}
    # canceling a finished run changes nothing
    assert env.call("POST", f"/runs/{run_id}/cancel").json()["status"] == "canceled"


def test_a_job_follows_latest_or_stays_pinned(env):
    p = new_pipeline(env, spec={"tasks": [task("a", "source", source="print('v1')\n")]})
    following = new_job(env, p["id"], "following")
    pinned = new_job(env, p["id"], "pinned", pipelineVersion=1)
    env.call("POST", f"/pipelines/{p['id']}/versions", json={"spec": {"tasks": [task("a", "source", source="print('v2')\n")]}})
    out = {}
    for j in (following, pinned):
        run = env.wait_run(env.call("POST", f"/jobs/{j['id']}/run").json()["id"])
        out[j["name"]] = (run["pipelineVersion"], [ln["message"] for ln in env.call("GET", f"/runs/{run['id']}/logs?task=a").json()["items"] if ln["stream"] == "stdout"])
    assert out == {"following": (2, ["v2"]), "pinned": (1, ["v1"])}


def test_deleting_things_respects_dependencies(env):
    p = new_pipeline(env)
    job = new_job(env, p["id"])
    r = env.call("DELETE", f"/pipelines/{p['id']}")
    assert r.status_code == 409 and "jobs" in r.json()["error"]
    run_id = env.call("POST", f"/jobs/{job['id']}/run").json()["id"]
    env.wait_run(run_id)
    assert env.call("DELETE", f"/jobs/{job['id']}").status_code == 204
    assert env.call("GET", f"/runs/{run_id}").status_code == 404  # its history goes with it
    assert env.call("DELETE", f"/pipelines/{p['id']}").status_code == 204
    assert env.call("DELETE", f"/pipelines/{p['id']}").status_code == 404


def test_deleting_a_job_mid_run_stops_the_run(env):
    slow = {"tasks": [task("a", "source", source="import time\ntime.sleep(60)\n")]}
    p = new_pipeline(env, spec=slow)
    job = new_job(env, p["id"])
    run_id = env.call("POST", f"/jobs/{job['id']}/run").json()["id"]
    import time

    time.sleep(1.0)
    t0 = time.monotonic()
    assert env.call("DELETE", f"/jobs/{job['id']}").status_code == 204
    env.client.__exit__(None, None, None)  # shutdown joins the worker: it must not be left running for 60s
    assert time.monotonic() - t0 < 20
    assert run_id


def test_huge_request_bodies_are_refused(env):
    r = env.client.post("/api/pipelines", content=b"{}", headers={"Authorization": "Bearer tok-editor", "X-Booth-Workspace": "acme", "Content-Type": "application/json", "Content-Length": str(64 * 1024 * 1024)})
    assert r.status_code == 413
