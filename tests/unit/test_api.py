"""The HTTP API end to end: auth, pipelines, tasks and runs — real subprocess execution, in-memory
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


def new_task(env: Env, name: str = "t", config=None, token: str = "tok-editor", **kw):
    r = env.call("POST", "/tasks", token, json={"name": name, "config": config, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def scheduled(env: Env, pipeline_id: str, cron: str = "0 9 * * *", **kw):
    r = env.call("PUT", f"/pipelines/{pipeline_id}/schedule", json={"schedule": {"cron": cron}, **kw})
    assert r.status_code == 200, r.text
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
    assert env.call("GET", "/pipelines", "tok-viewer").status_code == 200
    assert env.call("GET", "/tasks", "tok-viewer").status_code == 200
    assert env.call("GET", f"/pipelines/{p['id']}/versions/latest", "tok-viewer").status_code == 200
    for method, path, body in [
        ("POST", "/pipelines", {"name": "n"}),
        ("PUT", f"/pipelines/{p['id']}", {"name": "n"}),
        ("DELETE", f"/pipelines/{p['id']}", None),
        ("POST", f"/pipelines/{p['id']}/versions", {"spec": etl_spec()}),
        ("PUT", f"/pipelines/{p['id']}/schedule", {"schedule": {"cron": "0 9 * * *"}}),
        ("POST", f"/pipelines/{p['id']}/run", None),
        ("POST", "/tasks", {"name": "t"}),
    ]:
        assert env.call(method, path, "tok-viewer", json=body).status_code == 403, (method, path)


def test_another_workspace_cannot_see_or_touch_anything(env):
    p = new_pipeline(env)
    run_id = env.call("POST", f"/pipelines/{p['id']}/run").json()["id"]
    from booth_pipeline.auth import Claims

    from .harness import TOKENS

    TOKENS["tok-globex"] = Claims("u-g", "gail", ("/workspaces/globex/editor",))
    try:
        for path in (f"/pipelines/{p['id']}", f"/runs/{run_id}", f"/runs/{run_id}/logs"):
            assert env.call("GET", path, "tok-globex", ws="globex").status_code == 404, path
        assert env.call("GET", "/pipelines", "tok-globex", ws="globex").json()["total"] == 0
        assert env.call("POST", f"/pipelines/{p['id']}/run", "tok-globex", ws="globex").status_code == 404
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
    spec2["tasks"].append(task("audit", ["clean"]))
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


# ---- YAML export (ADR 0071 phase 4) ----------------------------------------------------------


def test_export_is_a_faithful_yaml_snapshot_of_one_version(env):
    import yaml

    p = new_pipeline(env)
    scheduled(env, p["id"], cron="0 9 * * *")
    r = env.call("GET", f"/pipelines/{p['id']}/versions/1/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/yaml")
    doc = yaml.safe_load(r.text)
    assert doc["apiVersion"] == "booth-pipeline/v1" and doc["kind"] == "Pipeline"
    assert doc["metadata"]["id"] == p["id"] and doc["metadata"]["version"] == 1
    assert doc["spec"]["schedule"] == {"type": "cron", "cron": "0 9 * * *", "timezone": "UTC", "enabled": True}
    assert [t["key"] for t in doc["spec"]["tasks"]] == ["extract", "clean", "load"]
    clean = next(t for t in doc["spec"]["tasks"] if t["key"] == "clean")
    assert clean["dependsOn"] == ["extract"]
    assert clean["task"]["name"] == "clean" and clean["task"]["version"] == 1
    assert clean["task"]["config"]["code"]["type"] == "inline"  # the task's real, resolved config — not just a reference


def test_export_an_old_version_is_immutable_even_after_a_newer_save(env):
    import yaml

    p = new_pipeline(env)
    spec2 = etl_spec()
    spec2["tasks"].append(task("audit", ["clean"]))
    env.call("POST", f"/pipelines/{p['id']}/versions", json={"spec": spec2})
    doc1 = yaml.safe_load(env.call("GET", f"/pipelines/{p['id']}/versions/1/export").text)
    doc2 = yaml.safe_load(env.call("GET", f"/pipelines/{p['id']}/versions/latest/export").text)
    assert len(doc1["spec"]["tasks"]) == 3 and len(doc2["spec"]["tasks"]) == 4


def test_export_of_an_unknown_version_is_404(env):
    p = new_pipeline(env)
    assert env.call("GET", f"/pipelines/{p['id']}/versions/9/export").status_code == 404


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
    bad = {"tasks": [task("a"), task("b", ["nope"])]}
    assert env.call("POST", "/pipelines", json={"name": "oops", "spec": bad}).status_code == 422
    assert env.call("GET", "/pipelines").json()["total"] == 0


@pytest.mark.parametrize(
    "spec, field, fragment",
    [
        ({"tasks": []}, "tasks", "at least one task"),
        ({"tasks": [task("a"), task("b", ["zzz"])]}, "tasks[1].dependsOn", "not a task"),
        ({"tasks": [task("a", ["b"]), task("b", ["a"])]}, "tasks", "cycle"),
        ({"tasks": [task("Bad Key")]}, "spec.tasks[0].key", "lowercase"),
    ],
)
def test_invalid_specs_are_422_naming_the_field(env, spec, field, fragment):
    p = new_pipeline(env, "host")
    r = env.call("POST", f"/pipelines/{p['id']}/versions", json={"spec": spec})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["field"] == field and fragment in body["error"]


def test_a_pipeline_referencing_a_task_that_does_not_exist_is_422(env):
    p = new_pipeline(env, "host")
    r = env.call("POST", f"/pipelines/{p['id']}/versions", json={"spec": {"tasks": [{"key": "a", "taskId": "no-such-task", "taskVersion": "latest", "dependsOn": [], "position": {"x": 0, "y": 0}}]}})
    assert r.status_code == 422 and r.json()["field"] == "tasks[0].taskId" and "does not exist" in r.json()["error"]


def test_a_field_constraint_violation_gets_a_clean_message_not_pydantics_own(env):
    """Regression for the 2026-09-23 pilot finding: switching a task's code-source radio before
    anything is picked sends an entryId/backendId of "" — a bare Field(min_length=1) violation —
    and the global RequestValidationError handler used to pass pydantic's own internal wording
    straight through ("String should have at least 1 character")."""
    bad = env.call("POST", "/tasks", json={"name": "a", "config": {"code": {"type": "catalog", "entryId": "", "version": "latest"}}})
    assert bad.status_code == 422
    assert bad.json()["error"] == "must not be empty"
    assert "String should have" not in bad.json()["error"]


def test_live_validation_endpoint_reports_without_saving(env):
    ok = env.call("POST", "/pipelines/validate", json={"spec": etl_spec()}).json()
    assert ok == {"valid": True}
    bad = env.call("POST", "/pipelines/validate", json={"spec": {"tasks": [task("a", ["ghost"])]}})
    assert bad.status_code == 200 and bad.json()["valid"] is False and bad.json()["field"] == "tasks[0].dependsOn"
    assert env.call("GET", "/pipelines").json()["total"] == 0


def test_deleting_things_respects_dependencies(env):
    p = new_pipeline(env)
    run_id = env.call("POST", f"/pipelines/{p['id']}/run").json()["id"]
    env.wait_run(run_id)
    assert env.call("DELETE", f"/pipelines/{p['id']}").status_code == 204
    assert env.call("GET", f"/runs/{run_id}").status_code == 404  # its history goes with it
    assert env.call("DELETE", f"/pipelines/{p['id']}").status_code == 404


def test_deleting_a_pipeline_mid_run_stops_the_run(env):
    slow = {"tasks": [task("a", source="import time\ntime.sleep(60)\n")]}
    p = new_pipeline(env, spec=slow)
    run_id = env.call("POST", f"/pipelines/{p['id']}/run").json()["id"]
    import time

    time.sleep(1.0)
    t0 = time.monotonic()
    assert env.call("DELETE", f"/pipelines/{p['id']}").status_code == 204
    env.client.__exit__(None, None, None)  # shutdown joins the worker: it must not be left running for 60s
    assert time.monotonic() - t0 < 20
    assert run_id


def test_huge_request_bodies_are_refused(env):
    r = env.client.post("/api/pipelines", content=b"{}", headers={"Authorization": "Bearer tok-editor", "X-Booth-Workspace": "acme", "Content-Type": "application/json", "Content-Length": str(64 * 1024 * 1024)})
    assert r.status_code == 413


# ---- tasks: CRUD + versioning (ADR 0071 — mirrors booth-catalog's own code versioning) --------


def test_task_versions_lifecycle(env):
    t = new_task(env, "loader", config={"code": {"type": "inline", "source": "x=1"}})
    assert t["latestVersion"] == 1 and t["version"]["version"] == 1
    r = env.call("POST", f"/tasks/{t['id']}/versions", json={"config": {"code": {"type": "inline", "source": "x=2"}}, "notes": "v2"})
    assert r.status_code == 201 and r.json()["version"] == 2
    latest = env.call("GET", f"/tasks/{t['id']}/versions/latest").json()
    assert latest["version"] == 2 and latest["config"]["code"]["source"] == "x=2" and latest["notes"] == "v2"
    v1 = env.call("GET", f"/tasks/{t['id']}/versions/1").json()
    assert v1["config"]["code"]["source"] == "x=1"  # old versions are immutable
    assert [v["version"] for v in env.call("GET", f"/tasks/{t['id']}/versions").json()["items"]] == [2, 1]
    assert env.call("GET", f"/tasks/{t['id']}/versions/9").status_code == 404
    assert env.call("GET", f"/tasks/{t['id']}").json()["latestVersion"] == 2


def test_empty_task_can_be_created_then_configured(env):
    r = env.call("POST", "/tasks", json={"name": "blank"})
    t = r.json()
    assert r.status_code == 201 and t["latestVersion"] == 0 and t["version"] is None
    assert env.call("GET", f"/tasks/{t['id']}/versions/latest").status_code == 404
    assert env.call("POST", f"/tasks/{t['id']}/versions", json={"config": {"code": {"type": "inline", "source": "x=1"}}}).status_code == 201


def test_task_names_need_not_be_unique(env):
    new_task(env, "dup")
    r = env.call("POST", "/tasks", json={"name": "dup"})
    assert r.status_code == 201  # unlike a pipeline's name, a task's is a browsable label, not an identity


def test_a_runner_that_is_not_available_is_refused_at_task_save(env):
    r = env.call("POST", "/tasks", json={"name": "t", "config": {"code": {"type": "inline", "source": "x=1"}, "runner": "spark"}})
    assert r.status_code == 422 and r.json()["field"] == "runner" and "not available" in r.json()["error"]


def test_deleting_a_task_still_referenced_by_a_pipeline_version_is_refused(env):
    p = new_pipeline(env)
    v = env.call("GET", f"/pipelines/{p['id']}/versions/1").json()
    task_id = v["spec"]["tasks"][0]["taskId"]
    r = env.call("DELETE", f"/tasks/{task_id}")
    assert r.status_code == 409
    assert env.call("DELETE", f"/pipelines/{p['id']}").status_code == 204
    assert env.call("DELETE", f"/tasks/{task_id}").status_code == 204  # free once nothing references it


def test_updating_a_tasks_own_name_and_description(env):
    t = new_task(env, "old", description="d1")
    r = env.call("PUT", f"/tasks/{t['id']}", json={"name": "new", "description": "d2"})
    assert r.status_code == 200 and r.json()["name"] == "new" and r.json()["description"] == "d2"
    assert env.call("GET", "/tasks?q=new").json()["total"] == 1


# ---- catalog code references (ADR 0010), resolved at TASK-version save time now (ADR 0071) ----


def test_catalog_latest_is_resolved_and_snapshotted_at_task_save(env):
    env.catalog.add("e1", "loader", {"1.0.0": "def run(ctx):\n    return 'v1'\n", "2.0.0": "def run(ctx):\n    return 'v2'\n"})
    t = new_task(env, "a", config={"code": {"type": "catalog", "entryId": "e1", "version": "latest"}})
    code = t["version"]["config"]["code"]
    import hashlib

    assert code["version"] == "2.0.0"  # "latest" pinned to the concrete label
    assert code["name"] == "loader" and code["entryId"] == "e1"
    assert code["source"] == "def run(ctx):\n    return 'v2'\n"
    assert code["sha256"] == hashlib.sha256(code["source"].encode()).hexdigest()


def test_runs_use_the_snapshot_and_never_call_the_catalog(env):
    env.catalog.add("e1", "loader", {"1.0.0": "print('from catalog v1')\n"})
    t = new_task(env, "a", config={"code": {"type": "catalog", "entryId": "e1", "version": "1.0.0"}})
    p = new_pipeline(env, spec={"tasks": [{"key": "a", "taskId": t["id"], "taskVersion": 1, "dependsOn": [], "position": {"x": 0, "y": 0}}]})
    env.catalog.requests.clear()
    env.catalog.down = True  # the catalog disappears entirely after save
    env.catalog.entries["e1"]["versions"]["1.0.0"] = "print('TAMPERED')\n"  # and even if it did change
    run = env.wait_run(env.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert run["status"] == "succeeded"
    logs = env.call("GET", f"/runs/{run['id']}/logs?task=a").json()["items"]
    assert [ln["message"] for ln in logs if ln["stream"] == "stdout"] == ["from catalog v1"]
    assert env.catalog.requests == []  # a run never touches the catalog


def test_catalog_call_carries_the_callers_own_token_and_workspace(env):
    env.catalog.add("e1", "loader", {"1.0.0": "pass\n"})
    new_task(env, "a", config={"code": {"type": "catalog", "entryId": "e1", "version": "1.0.0"}})
    assert env.catalog.requests
    for req in env.catalog.requests:
        assert req.headers["authorization"] == "Bearer tok-editor" and req.headers["x-workspace"] == "acme"
        assert req.url.path.startswith("/modules/catalog/api/code/e1")


def test_a_client_supplied_snapshot_is_never_trusted(env):
    env.catalog.add("e1", "loader", {"1.0.0": "print('real')\n"})
    forged = {"code": {"type": "catalog", "entryId": "e1", "version": "1.0.0", "source": "print('FORGED')\n", "sha256": "0" * 64, "name": "trust me"}}
    t = new_task(env, "a", config=forged)
    code = t["version"]["config"]["code"]
    assert code["source"] == "print('real')\n" and code["name"] == "loader"


def test_a_new_task_version_reuses_the_previous_snapshot_while_the_catalog_is_down(env):
    env.catalog.add("e1", "loader", {"1.0.0": "pass\n"})
    t = new_task(env, "a", config={"code": {"type": "catalog", "entryId": "e1", "version": "1.0.0"}})
    env.catalog.down = True
    # only the previously-resolved reference is re-sent: reuses OUR stored snapshot, no catalog call
    r = env.call("POST", f"/tasks/{t['id']}/versions", json={"config": {"code": {"type": "catalog", "entryId": "e1", "version": "1.0.0"}}})
    assert r.status_code == 201, r.text
    # but a *new* catalog reference cannot be resolved while it is down:
    r2 = env.call("POST", f"/tasks/{t['id']}/versions", json={"config": {"code": {"type": "catalog", "entryId": "e2", "version": "1.0.0"}}})
    assert r2.status_code == 503


def test_a_pipeline_version_needs_no_catalog_or_storage_call_at_all(env):
    """ADR 0071: a task's code is already snapshotted when its own version is saved, so saving a
    PIPELINE version — which only references (taskId, taskVersion) pairs — never touches the
    catalog or storage, unconditionally, not just when reusing a previous reference."""
    env.catalog.add("e1", "loader", {"1.0.0": "pass\n"})
    t = new_task(env, "a", config={"code": {"type": "catalog", "entryId": "e1", "version": "1.0.0"}})
    p = new_pipeline(env, spec={"tasks": [{"key": "a", "taskId": t["id"], "taskVersion": 1, "dependsOn": [], "position": {"x": 0, "y": 0}}]})
    env.catalog.requests.clear()
    env.catalog.down = True
    r = env.call("POST", f"/pipelines/{p['id']}/versions", json={"spec": {"tasks": [{"key": "a", "taskId": t["id"], "taskVersion": 1, "dependsOn": [], "position": {"x": 0, "y": 0}}]}})
    assert r.status_code == 201, r.text
    assert env.catalog.requests == []


def test_catalog_failure_modes_map_to_useful_statuses(env):
    save = lambda config: env.call("POST", "/tasks", json={"name": "t", "config": config})  # noqa: E731
    r = save({"code": {"type": "catalog", "entryId": "nope", "version": "latest"}})
    assert r.status_code == 422 and r.json()["field"] == "code" and "not found" in r.json()["error"]
    env.catalog.add("e1", "loader", {"1.0.0": "pass\n"})
    assert save({"code": {"type": "catalog", "entryId": "e1", "version": "9.9.9"}}).status_code == 422
    env.catalog.not_installed = True  # the gateway's plain-text 404 is NOT "entry not found"
    r = save({"code": {"type": "catalog", "entryId": "e1", "version": "1.0.0"}})
    assert r.status_code == 503 and "not installed" in r.json()["error"] and "Inline code needs no catalog" in r.json()["error"]
    env.catalog.not_installed, env.catalog.down = False, True
    assert save({"code": {"type": "catalog", "entryId": "e1", "version": "1.0.0"}}).status_code == 503


def test_sql_catalog_code_is_accepted_and_actually_runs_on_the_base_runner(env):
    """ADR 0064: the base runner's language dispatch is a real registry now, not a hardcoded
    Python-only check — SQL is a second supported language, not just tolerated at save time."""
    env.catalog.add("q", "report", {"1": "SELECT 1 AS x"}, language="sql")
    t = new_task(env, "a", config={"code": {"type": "catalog", "entryId": "q", "version": "1"}})
    p = new_pipeline(env, spec={"tasks": [{"key": "a", "taskId": t["id"], "taskVersion": 1, "dependsOn": [], "position": {"x": 0, "y": 0}}]})
    assert env.wait_run(env.call("POST", f"/pipelines/{p['id']}/run").json()["id"])["status"] == "succeeded"


def test_a_language_the_base_runner_has_no_strategy_for_is_still_refused(env):
    env.catalog.add("q", "report", {"1": "object Report"}, language="scala")
    r = env.call("POST", "/tasks", json={"name": "t", "config": {"code": {"type": "catalog", "entryId": "q", "version": "1"}}})
    assert r.status_code == 422 and "the base runner supports" in r.json()["error"] and "python" in r.json()["error"] and "sql" in r.json()["error"]


# ---- storage code references (ADR 0063) -----------------------------------------------------


def test_storage_code_is_resolved_and_snapshotted_at_task_save(env):
    env.storage.add("b1", "tasks/a.py", "def run(ctx):\n    return 'from storage'\n")
    t = new_task(env, "a", config={"code": {"type": "storage", "backendId": "b1", "path": "tasks/a.py"}})
    code = t["version"]["config"]["code"]
    import hashlib

    assert code["backendId"] == "b1" and code["path"] == "tasks/a.py"
    assert code["name"] == "a.py" and code["language"] == "python"  # inferred from the extension
    assert code["source"] == "def run(ctx):\n    return 'from storage'\n"
    assert code["sha256"] == hashlib.sha256(code["source"].encode()).hexdigest()


def test_runs_use_the_storage_snapshot_and_never_call_storage_again(env):
    env.storage.add("b1", "tasks/a.py", "print('from storage v1')\n")
    t = new_task(env, "a", config={"code": {"type": "storage", "backendId": "b1", "path": "tasks/a.py"}})
    p = new_pipeline(env, spec={"tasks": [{"key": "a", "taskId": t["id"], "taskVersion": 1, "dependsOn": [], "position": {"x": 0, "y": 0}}]})
    env.storage.requests.clear()
    env.storage.down = True  # storage disappears entirely after save
    env.storage.objects["b1"]["tasks/a.py"] = "print('TAMPERED')\n"  # and even if the file changed
    run = env.wait_run(env.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert run["status"] == "succeeded"
    logs = env.call("GET", f"/runs/{run['id']}/logs?task=a").json()["items"]
    assert [ln["message"] for ln in logs if ln["stream"] == "stdout"] == ["from storage v1"]
    assert env.storage.requests == []  # a run never touches storage for its own code


def test_storage_call_carries_the_callers_own_token_and_workspace(env):
    env.storage.add("b1", "tasks/a.py", "pass\n")
    new_task(env, "a", config={"code": {"type": "storage", "backendId": "b1", "path": "tasks/a.py"}})
    assert env.storage.requests
    for req in env.storage.requests:
        assert req.headers["authorization"] == "Bearer tok-editor" and req.headers["x-workspace"] == "acme"
        assert req.url.path.startswith("/modules/storage/api/backends/b1/objects/")


def test_a_client_supplied_storage_snapshot_is_never_trusted(env):
    env.storage.add("b1", "tasks/a.py", "print('real')\n")
    forged = {"code": {"type": "storage", "backendId": "b1", "path": "tasks/a.py", "source": "print('FORGED')\n", "sha256": "0" * 64, "name": "trust me"}}
    t = new_task(env, "a", config=forged)
    code = t["version"]["config"]["code"]
    assert code["source"] == "print('real')\n" and code["name"] == "a.py"


def test_storage_failure_modes_map_to_useful_statuses(env):
    save = lambda config: env.call("POST", "/tasks", json={"name": "t", "config": config})  # noqa: E731
    r = save({"code": {"type": "storage", "backendId": "b1", "path": "nope.py"}})
    assert r.status_code == 422 and r.json()["field"] == "code" and "no object" in r.json()["error"]
    env.storage.not_installed = True  # the gateway's plain-text 404 is NOT "object not found"
    r = save({"code": {"type": "storage", "backendId": "b1", "path": "also-nope.py"}})
    assert r.status_code == 503 and "not installed" in r.json()["error"] and "Inline code needs no storage" in r.json()["error"]
    env.storage.not_installed, env.storage.down = False, True
    assert save({"code": {"type": "storage", "backendId": "b1", "path": "still-nope.py"}}).status_code == 503


def test_inline_code_works_with_the_catalog_entirely_absent(env):
    """The brief's hard requirement: the base runner needs zero other modules."""
    env.catalog.not_installed = True
    p = new_pipeline(env)
    assert env.wait_run(env.call("POST", f"/pipelines/{p['id']}/run").json()["id"])["status"] == "succeeded"
    assert env.catalog.requests == []


# ---- scheduling & runs ------------------------------------------------------------------------


def test_scheduling_needs_a_saved_version_and_validates_the_trigger(env):
    blank = env.call("POST", "/pipelines", json={"name": "blank"}).json()
    r = env.call("PUT", f"/pipelines/{blank['id']}/schedule", json={"schedule": {"cron": "0 9 * * *"}})
    assert r.status_code == 422 and r.json()["field"] == "schedule"
    p = new_pipeline(env)
    bad = env.call("PUT", f"/pipelines/{p['id']}/schedule", json={"schedule": {"cron": "every day"}})
    assert bad.status_code == 422 and bad.json()["field"] == "schedule.cron"
    bad = env.call("PUT", f"/pipelines/{p['id']}/schedule", json={"schedule": {"cron": "0 9 * * *", "timezone": "Mars/Base"}})
    assert bad.status_code == 422


def test_schedule_is_persisted_and_re_editable(env):
    p = new_pipeline(env)
    sched = scheduled(env, p["id"], cron="0 9 * * 1-5")
    assert sched["nextRunAt"] and sched["schedule"] == {"type": "cron", "cron": "0 9 * * 1-5", "timezone": "UTC", "enabled": True}
    got = env.call("GET", f"/pipelines/{p['id']}").json()
    assert got["schedule"]["cron"] == "0 9 * * 1-5"
    r = env.call("PUT", f"/pipelines/{p['id']}/schedule", json={"schedule": {"cron": "*/5 * * * *", "enabled": False}, "allowConcurrentRuns": True})
    upd = r.json()
    assert upd["schedule"]["enabled"] is False and upd["nextRunAt"] is None  # disabled: no next fire
    assert upd["allowConcurrentRuns"] is True


def test_run_now_executes_the_dag_and_records_per_task_state(env):
    p = new_pipeline(env)
    r = env.call("POST", f"/pipelines/{p['id']}/run")
    assert r.status_code == 202 and r.json()["trigger"] == "manual" and r.json()["triggeredBy"] == "eddie"
    run = env.wait_run(r.json()["id"])
    assert run["status"] == "succeeded" and run["error"] is None and run["pipelineVersion"] == 1
    assert run["startedAt"] and run["finishedAt"]
    tasks = {t["taskKey"]: t for t in run["tasks"]}
    assert {k: t["status"] for k, t in tasks.items()} == {"extract": "succeeded", "clean": "succeeded", "load": "succeeded"}
    assert all(t["attempts"] == 1 and t["startedAt"] and t["finishedAt"] for t in tasks.values())
    assert env.call("GET", f"/runs?pipelineId={p['id']}").json()["total"] == 1


def test_run_logs_per_run_per_task_with_a_polling_cursor(env):
    p = new_pipeline(env)
    run = env.wait_run(env.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
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
    spec = {"tasks": [task("a"), task("boom", ["a"], source="raise ValueError('bad row 7')\n"), task("z", ["boom"])]}
    p = new_pipeline(env, spec=spec)
    run = env.wait_run(env.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert run["status"] == "failed" and "boom" in run["error"]
    assert {t["taskKey"]: t["status"] for t in run["tasks"]} == {"a": "succeeded", "boom": "failed", "z": "skipped"}
    assert any("ValueError: bad row 7" in ln["message"] for ln in env.call("GET", f"/runs/{run['id']}/logs?task=boom").json()["items"])


def test_a_tasks_own_retry_override_reruns_it(env):
    flaky = "def run(ctx):\n    if ctx.attempt < 3:\n        raise RuntimeError('flaky attempt %d' % ctx.attempt)\n    return 'ok'\n"
    spec = {"tasks": [task("a", source=flaky, retry={"maxRetries": 2}), task("b", ["a"])]}
    p = new_pipeline(env, spec=spec)
    run = env.wait_run(env.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert run["status"] == "succeeded"
    assert {t["taskKey"]: (t["status"], t["attempts"]) for t in run["tasks"]}["a"] == ("succeeded", 3)
    # with no retry configured at all the same code simply fails
    p2 = new_pipeline(env, "p2", spec={"tasks": [task("a", source=flaky)]})
    assert env.wait_run(env.call("POST", f"/pipelines/{p2['id']}/run").json()["id"])["status"] == "failed"


def test_a_non_concurrent_pipeline_refuses_a_second_active_run_and_a_concurrent_one_allows_it(env):
    slow = {"tasks": [task("a", source="import time\ntime.sleep(3)\n")]}
    p = new_pipeline(env, spec=slow)
    first = env.call("POST", f"/pipelines/{p['id']}/run")
    assert first.status_code == 202
    second = env.call("POST", f"/pipelines/{p['id']}/run")
    assert second.status_code == 409 and "active run" in second.json()["error"]
    env.wait_run(first.json()["id"])
    assert env.call("POST", f"/pipelines/{p['id']}/run").status_code == 202  # free again
    conc = new_pipeline(env, "conc", spec=slow)
    scheduled(env, conc["id"], allowConcurrentRuns=True)
    ids = [env.call("POST", f"/pipelines/{conc['id']}/run").json()["id"] for _ in range(2)]
    for i in ids:
        env.wait_run(i)


def test_a_running_run_can_be_canceled(env):
    slow = {"tasks": [task("a", source="import time\nprint('working', flush=True)\ntime.sleep(60)\n"), task("b", ["a"])]}
    p = new_pipeline(env, spec=slow)
    run_id = env.call("POST", f"/pipelines/{p['id']}/run").json()["id"]
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


def test_a_run_follows_latest_by_default(env):
    p = new_pipeline(env, spec={"tasks": [task("a", source="print('v1')\n")]})
    run1 = env.wait_run(env.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert run1["pipelineVersion"] == 1
    env.call("POST", f"/pipelines/{p['id']}/versions", json={"spec": {"tasks": [task("a", source="print('v2')\n")]}})
    run2 = env.wait_run(env.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert run2["pipelineVersion"] == 2
    msgs = [ln["message"] for ln in env.call("GET", f"/runs/{run2['id']}/logs?task=a").json()["items"] if ln["stream"] == "stdout"]
    assert msgs == ["v2"]


def test_a_pinned_version_stays_pinned_even_as_newer_versions_are_saved(env):
    """ADR 0071's second "Open question, ruled 2026-09-24": mirrors the old Job's
    pipeline_version pin, now living on Pipeline directly."""
    p = new_pipeline(env, spec={"tasks": [task("a", source="print('v1')\n")]})
    sched = env.call("PUT", f"/pipelines/{p['id']}/schedule", json={"pinnedVersion": 1})
    assert sched.status_code == 200 and sched.json()["pinnedVersion"] == 1
    env.call("POST", f"/pipelines/{p['id']}/versions", json={"spec": {"tasks": [task("a", source="print('v2')\n")]}})
    run = env.wait_run(env.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert run["pipelineVersion"] == 1  # still pinned, despite v2 now being latest
    msgs = [ln["message"] for ln in env.call("GET", f"/runs/{run['id']}/logs?task=a").json()["items"] if ln["stream"] == "stdout"]
    assert msgs == ["v1"]
    # unpinning goes back to following latest
    env.call("PUT", f"/pipelines/{p['id']}/schedule", json={"pinnedVersion": None})
    run2 = env.wait_run(env.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert run2["pipelineVersion"] == 2


def test_pinning_a_version_that_does_not_exist_is_422(env):
    p = new_pipeline(env)
    r = env.call("PUT", f"/pipelines/{p['id']}/schedule", json={"pinnedVersion": 9})
    assert r.status_code == 422 and r.json()["field"] == "pinnedVersion"
