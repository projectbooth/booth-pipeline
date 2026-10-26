"""Task secrets (ADR 0128 and its Amendment 1): sealing, the key, what a task may declare, the owner's
API, delivery to the task's own process, and, above all, that a value never reaches anything the module
stores or shows: logs, outputs, errors, exports, invocation.json, a Kubernetes object, or a log line of
the runner service."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from datetime import UTC, datetime

import pytest

from booth_pipeline.config import Config, ConfigError
from booth_pipeline.engine import execute
from booth_pipeline.model import ModelError, TaskConfig, validate_task_config
from booth_pipeline.records import TaskSecret
from booth_pipeline.runner_service import create_runner_app
from booth_pipeline.runners.base import Cancellation, TaskInvocation
from booth_pipeline.runners.registry import RunnerRegistry
from booth_pipeline.runners.remote import RemoteRunner
from booth_pipeline.runners.subprocess_runner import SubprocessRunner
from booth_pipeline.store.memory import MemoryStore
from booth_pipeline.task_secrets import (
    Redactor,
    Sealed,
    SecretBox,
    SecretsError,
    SecretSource,
    SecretUnreadable,
    check_value,
    reseal,
)

from .harness import make_env, task
from .helpers import ListRecorder, build, node
from .servers import serve_asgi

VALUE = "fmp-live-0123456789abcdef"  # what must never be stored anywhere
OTHER = "polygon-key-ZYXWVUTSRQ"
KEY = os.urandom(32)


def box(**keys: bytes) -> SecretBox:
    keys = keys or {"k1": KEY}
    return SecretBox(keys, next(iter(keys)))


def put(store, b: SecretBox, name: str, value: str, ws: str = "acme") -> None:
    s = b.seal(ws, name, value)
    store.put_task_secret(TaskSecret(ws, name, s.ciphertext, s.nonce, s.key_id, "olga", datetime.now(UTC)))


# ---- sealing and the key --------------------------------------------------------------------------


def test_a_value_opens_only_for_its_own_workspace_and_name():
    b = box()
    sealed = b.seal("acme", "FMP", VALUE)
    assert VALUE.encode() not in sealed.ciphertext
    assert b.open("acme", "FMP", sealed) == VALUE
    with pytest.raises(SecretUnreadable):
        b.open("globex", "FMP", sealed)  # copied to another workspace: refused
    with pytest.raises(SecretUnreadable):
        b.open("acme", "POLYGON", sealed)  # or another name
    assert b.seal("acme", "FMP", VALUE).nonce != sealed.nonce  # a fresh nonce every time


def test_a_value_sealed_with_a_key_that_is_gone_says_to_re_enter_it():
    sealed = box(old=os.urandom(32)).seal("acme", "FMP", VALUE)
    with pytest.raises(SecretUnreadable, match="no longer mounted; re-enter it"):
        box().open("acme", "FMP", sealed)


def test_the_key_directory(tmp_path):
    assert SecretBox.from_dir(str(tmp_path / "absent")) is None
    assert SecretBox.from_dir(str(tmp_path)) is None  # mounted but empty: off
    (tmp_path / "k1").write_text(base64.b64encode(KEY).decode())
    (tmp_path / "..data").mkdir()  # a mounted Secret's own entries are ignored
    assert SecretBox.from_dir(str(tmp_path)).current == "k1"
    (tmp_path / "k2").write_text(base64.b64encode(os.urandom(32)).decode() + "\n")
    with pytest.raises(SecretsError, match="several task secrets keys"):
        SecretBox.from_dir(str(tmp_path))
    assert SecretBox.from_dir(str(tmp_path), "k2").current == "k2"
    (tmp_path / "k3").write_text(base64.b64encode(b"short").decode())
    with pytest.raises(SecretsError, match="must be 32"):
        SecretBox.from_dir(str(tmp_path), "k2")
    (tmp_path / "k3").write_text("not base64!")
    with pytest.raises(SecretsError, match="not base64"):
        SecretBox.from_dir(str(tmp_path), "k2")


def test_startup_refuses_a_key_when_tasks_would_run_in_this_pod(tmp_path):
    (tmp_path / "k1").write_text(base64.b64encode(KEY).decode())
    base = {"dev_memory": True, "oidc_issuer_url": "https://idp/x", "oidc_require_audience": False, "secrets_key_dir": str(tmp_path)}
    with pytest.raises(ConfigError, match="tasks would run in the same pod"):
        Config(**base).validate()
    Config(**base, task_job_template="/etc/job.json").validate()
    (tmp_path / "k1").write_text("nope")
    with pytest.raises(ConfigError, match="task secrets key"):
        Config(**base, task_job_template="/etc/job.json").validate()


def test_rotation_re_seals_with_the_current_key_and_leaves_what_it_cannot_open(caplog):
    store, old, lost = MemoryStore(), os.urandom(32), os.urandom(32)
    put(store, SecretBox({"k1": old}, "k1"), "FMP", VALUE)
    put(store, SecretBox({"gone": lost}, "gone"), "POLY", OTHER)
    b = SecretBox({"k1": old, "k2": KEY}, "k2")
    with caplog.at_level(logging.WARNING):
        reseal(store, b)
    row = store.get_task_secret("acme", "FMP")
    assert row.key_id == "k2" and SecretBox({"k2": KEY}, "k2").open("acme", "FMP", Sealed(row.ciphertext, row.nonce, row.key_id)) == VALUE
    assert store.get_task_secret("acme", "POLY").key_id == "gone"
    assert "1 task secret(s) were sealed with a key that is no longer mounted" in caplog.text
    assert VALUE not in caplog.text and OTHER not in caplog.text


@pytest.mark.parametrize(
    ("value", "problem"),
    [("short", "at least 8"), ("1234567", "at least 8"), (" padded-value ", "whitespace"), ("line1\nline2xx", "one line"), ("x" * 9000, "at most")],
)
def test_values_that_cannot_be_kept_safely_are_refused(value, problem):
    assert problem in check_value(value)


def test_eight_characters_is_enough():
    assert check_value("12345678") is None


# ---- redaction ----------------------------------------------------------------------------------


def test_redaction_replaces_every_exact_value_longest_first_in_text_and_json():
    r = Redactor()
    assert not r and r.text(VALUE) == VALUE
    r.add({"FMP": VALUE, "PART": VALUE[:12]})
    assert r.text(f"key={VALUE}; again {VALUE}") == "key=[secret FMP]; again [secret FMP]"
    assert r.text(VALUE[:12] + "!") == "[secret PART]!"
    assert r.value({VALUE: [VALUE, 1, {"k": f"x{VALUE}"}], "n": None}) == {"[secret FMP]": ["[secret FMP]", 1, {"k": "x[secret FMP]"}], "n": None}


# ---- what a task may declare ------------------------------------------------------------------------


def cfg(**kw) -> TaskConfig:
    return TaskConfig.model_validate({"code": {"type": "inline", "source": "pass\n"}, **kw})


def test_a_python_base_task_declares_secrets_by_name_and_environment_variable():
    validate_task_config(cfg(secrets=[{"name": "FMP", "env": "FMP_API_KEY"}, {"name": "fmp-backup", "env": "FMP_BACKUP"}]))


@pytest.mark.parametrize(
    ("secrets", "field", "message"),
    [
        ([{"name": "FMP", "env": "PATH"}], "secrets[0].env", "set by the runner"),
        ([{"name": "FMP", "env": "BOOTH_TOKEN_FILE"}], "secrets[0].env", "set by the runner"),
        ([{"name": "FMP", "env": "PYTHONPATH"}], "secrets[0].env", "set by the runner"),
        ([{"name": "FMP", "env": "AWS_SECRET_ACCESS_KEY"}], "secrets[0].env", "set by the runner"),
        ([{"name": "FMP", "env": "DATABASE_URL"}], "secrets[0].env", "set by the runner"),
        ([{"name": "FMP", "env": "fmp_key"}], "secrets[0].env", "upper case"),
        ([{"name": "FMP", "env": "1KEY"}], "secrets[0].env", "upper case"),
        ([{"name": "has space", "env": "K"}], "secrets[0].name", "a letter"),
        ([{"name": "A", "env": "K"}, {"name": "B", "env": "K"}], "secrets[1].env", "given twice"),
    ],
)
def test_bad_declarations_are_refused_at_save(secrets, field, message):
    with pytest.raises(ModelError, match=message) as e:
        validate_task_config(cfg(secrets=secrets))
    assert e.value.field == field


def test_secrets_are_for_base_python_tasks_only():
    spark = cfg(runner="spark", spark={}, secrets=[{"name": "FMP", "env": "FMP_API_KEY"}])
    with pytest.raises(ModelError, match="base tasks only"):
        validate_task_config(spark)
    sql = TaskConfig.model_validate({"code": {"type": "catalog", "entryId": "e", "version": "1", "language": "sql", "source": "select 1"}, "secrets": [{"name": "FMP", "env": "K"}]})
    with pytest.raises(ModelError, match="only a Python task"):
        validate_task_config(sql)


# ---- the engine: delivery, a missing secret, and redaction of what it reports --------------------


# The task's own source never holds the value (a source is stored and exported): it compares a hash.
LEAKY = f"""
import hashlib, os
def run(ctx):
    print("env", hashlib.sha256(os.environ["FMP_API_KEY"].encode()).hexdigest() == {hashlib.sha256(VALUE.encode()).hexdigest()!r})
    print("invocation.json clean", os.environ["FMP_API_KEY"] not in open("invocation.json").read())
    print("the key is", os.environ["FMP_API_KEY"])
    ctx.log.warning("in stderr too: %s", os.environ["FMP_API_KEY"])
    return {{"key": os.environ["FMP_API_KEY"], os.environ["FMP_API_KEY"]: [1]}}
"""


def engine_run(nodes, store, b=None):
    spec, configs = build(*nodes)
    rec = ListRecorder()
    res = execute(spec, configs, run_id="r1", registry=RunnerRegistry([SubprocessRunner()]), recorder=rec, cancel=Cancellation(), secrets=SecretSource(store, b or box(), "acme"))
    return res, rec


def test_a_declared_secret_reaches_the_task_process_only_as_its_environment_variable():
    store, b = MemoryStore(), box()
    put(store, b, "FMP", VALUE)
    res, rec = engine_run([node("a", source=LEAKY, secrets=[{"name": "FMP", "env": "FMP_API_KEY"}])], store, b)
    assert res.success, res.error
    out = rec.log_text("a")
    assert "env True" in out and "invocation.json clean True" in out
    assert ("secrets", "a", 1, ["FMP"]) in rec.events


def test_a_missing_secret_fails_that_task_only_and_is_not_retried():
    store = MemoryStore()
    res, rec = engine_run(
        [
            node("needs", secrets=[{"name": "FMP", "env": "FMP_API_KEY"}], retry={"maxRetries": 3, "delaySeconds": 0}),
            node("after", ["needs"]),
            node("independent"),
        ],
        store,
    )
    assert not res.success and res.error == "task(s) failed: needs: secret FMP is not set in this workspace"
    assert rec.statuses("needs") == ["started", "failed"]  # one attempt: an owner has to set it
    assert rec.statuses("after") == [] and rec.statuses("independent") == ["started", "succeeded"]


def test_a_task_that_declares_no_secret_gets_none_and_never_reads_the_store():
    class Untouchable:
        def get_task_secret(self, *_):
            raise AssertionError("read the store for a task with no secrets")

    spec, configs = build(node("a", source="import os\ndef run(ctx):\n    return sorted(k for k in os.environ if 'FMP' in k)\n"))
    rec = ListRecorder()
    res = execute(spec, configs, run_id="r1", registry=RunnerRegistry([SubprocessRunner()]), recorder=rec, cancel=Cancellation(), secrets=SecretSource(Untouchable(), box(), "acme"))
    assert res.success


def test_without_a_key_a_task_that_declares_secrets_fails_saying_what_to_set():
    spec, configs = build(node("a", secrets=[{"name": "FMP", "env": "FMP_API_KEY"}]))
    rec = ListRecorder()
    res = execute(spec, configs, run_id="r1", registry=RunnerRegistry([SubprocessRunner()]), recorder=rec, cancel=Cancellation(), secrets=SecretSource(MemoryStore(), None, "acme"))
    assert "taskSecrets.keySecretName" in res.error


def test_a_failure_message_carrying_the_value_is_redacted_before_dagster_or_the_run_sees_it(caplog):
    store, b = MemoryStore(), box()
    put(store, b, "FMP", VALUE)

    class Leaks:
        id = "base"

        def run(self, inv: TaskInvocation, log, cancel):
            raise RuntimeError(f"upstream said: bad key {inv.env['FMP_API_KEY']}")

    spec, configs = build(node("a", secrets=[{"name": "FMP", "env": "FMP_API_KEY"}], retry={"maxRetries": 1, "delaySeconds": 0}))
    rec = ListRecorder()
    with caplog.at_level(logging.DEBUG):
        res = execute(spec, configs, run_id="r1", registry=RunnerRegistry([Leaks()]), recorder=rec, cancel=Cancellation(), secrets=SecretSource(store, b, "acme"))
    assert res.error == "task(s) failed: a: upstream said: bad key [secret FMP]"
    assert VALUE not in caplog.text  # Dagster's own step-failure log in this pod
    assert VALUE not in repr(rec.events)


# ---- end to end through the API: nothing stored or shown carries the value ------------------------


@pytest.fixture()
def env():
    e = make_env(secret_box=box())
    yield e
    e.client.close()


def test_only_owners_set_and_delete_editors_list_names_viewers_see_nothing(env):
    assert env.call("PUT", "/secrets/FMP", "tok-editor", json={"value": VALUE}).status_code == 403
    r = env.call("PUT", "/secrets/FMP", "tok-owner", json={"value": VALUE})
    assert r.status_code == 200 and r.json()["name"] == "FMP" and r.json()["updatedBy"] == "olga"
    assert VALUE not in r.text
    listed = env.call("GET", "/secrets", "tok-editor")
    assert [s["name"] for s in listed.json()["items"]] == ["FMP"] and VALUE not in listed.text
    assert set(listed.json()["items"][0]) == {"name", "updatedBy", "updatedAt"}
    assert env.call("GET", "/secrets", "tok-viewer").status_code == 403
    assert env.call("DELETE", "/secrets/FMP", "tok-editor").status_code == 403
    assert env.call("DELETE", "/secrets/FMP", "tok-owner").status_code == 204
    assert env.call("DELETE", "/secrets/FMP", "tok-owner").status_code == 404
    assert env.call("GET", "/secrets", "tok-owner").json()["items"] == []


def test_a_short_value_is_refused_without_echoing_it(env):
    r = env.call("PUT", "/secrets/FMP", "tok-owner", json={"value": "abc1234"})
    assert r.status_code == 422 and "at least 8" in r.text and "abc1234" not in r.text
    assert env.call("PUT", "/secrets/bad name", "tok-owner", json={"value": VALUE}).status_code in (404, 422)


def test_without_a_key_the_secrets_api_says_it_is_not_set_up():
    e = make_env()
    try:
        r = e.call("PUT", "/secrets/FMP", "tok-owner", json={"value": VALUE})
        assert r.status_code == 503 and "taskSecrets.keySecretName" in r.text
    finally:
        e.client.close()


def test_no_value_reaches_logs_outputs_errors_exports_or_the_run_view(env):
    env.call("PUT", "/secrets/FMP", "tok-owner", json={"value": VALUE})
    failing = "import os\ndef run(ctx):\n    raise RuntimeError('rejected ' + os.environ['FMP_API_KEY'])\n"
    spec = {
        "tasks": [
            task("capture", source=LEAKY, secrets=[{"name": "FMP", "env": "FMP_API_KEY"}]),
            task("echo", ["capture"], source="def run(ctx):\n    print(ctx.inputs)\n    return ctx.inputs\n"),
            task("fails", source=failing, secrets=[{"name": "FMP", "env": "FMP_API_KEY"}]),
            task("missing", secrets=[{"name": "POLYGON", "env": "POLYGON_KEY"}]),
        ]
    }
    p = env.call("POST", "/pipelines", json={"name": "capture", "spec": spec}).json()
    run = env.wait_run(env.call("POST", f"/pipelines/{p['id']}/run").json()["id"])
    assert run["status"] == "failed"
    tasks = {t["taskKey"]: t for t in run["tasks"]}
    assert tasks["capture"]["status"] == "succeeded" and tasks["capture"]["secrets"] == ["FMP"]
    assert tasks["capture"]["output"] == {"value": {"key": "[secret FMP]", "[secret FMP]": [1]}}
    assert tasks["echo"]["output"]["value"] == {"capture": {"key": "[secret FMP]", "[secret FMP]": [1]}}
    assert tasks["missing"]["error"] == "secret POLYGON is not set in this workspace" and tasks["missing"]["secrets"] == []
    logs = env.call("GET", f"/runs/{run['id']}/logs?limit=1000").json()["items"]
    text = "\n".join(ln["message"] for ln in logs)
    assert "the key is [secret FMP]" in text and "in stderr too: [secret FMP]" in text and "rejected [secret FMP]" in text
    assert "env True" in text and "invocation.json clean True" in text
    # Everything the module would show or hand out about this run and pipeline:
    shown = [
        env.call("GET", f"/runs/{run['id']}").text,
        env.call("GET", "/runs").text,
        json.dumps(logs),
        env.call("GET", f"/pipelines/{p['id']}/versions/1/export").text,
        env.call("GET", "/secrets", "tok-owner").text,
    ]
    for t in env.call("GET", "/tasks").json()["items"]:
        shown.append(env.call("GET", f"/tasks/{t['id']}/versions/1").text)
    assert "FMP" in shown[3]  # the export names the secret...
    assert not any(VALUE in s for s in shown)  # ...and nothing carries its value
    # And nothing the store holds, sealed rows aside:
    stored = repr((env.store._runs, env.store._task_runs, env.store._logs, env.store._task_versions, env.store._versions))
    assert VALUE not in stored


# ---- the runner service and the task Job ----------------------------------------------------------


def test_the_runner_service_never_logs_a_request_body(caplog):
    with caplog.at_level(logging.DEBUG), serve_asgi(create_runner_app("s3cret-runner-bearer", SubprocessRunner(), max_concurrent=1)) as url:
        lines: list[tuple[str, str]] = []

        class L:
            def line(self, stream, message):
                lines.append((stream, message))

        src = "import os\ndef run(ctx):\n    return os.environ['FMP_API_KEY'] == " + repr(VALUE) + "\n"
        inv = TaskInvocation("r1", "t", "", 1, src, {}, {}, 60, env={"FMP_API_KEY": VALUE})
        assert RemoteRunner(url, "s3cret-runner-bearer").run(inv, L(), Cancellation()) is True
        bad = TaskInvocation("r1", "t", "", 1, "def run(ctx):\n    raise ValueError(1)\n", {}, {}, 60, env={"FMP_API_KEY": VALUE})
        with pytest.raises(Exception):  # noqa: B017 - any failure: the point is what got logged
            RemoteRunner(url, "s3cret-runner-bearer").run(bad, L(), Cancellation())
    assert caplog.records  # the service did log (requests, the task's lifecycle)...
    assert VALUE not in caplog.text  # ...never a value
    plain = TaskInvocation("r1", "t", "", 1, "pass\n", {}, {}, 60, env={"FMP_API_KEY": VALUE})
    assert "env" not in repr(plain) and VALUE not in repr(plain)  # never in a traceback or a debug line


def test_a_task_without_secrets_sends_no_env_field_so_an_older_runner_still_takes_it():
    import httpx

    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, content=b'{"t":"result","output":1}\n')

    r = RemoteRunner("http://runner.test", "bearer-bearer-bearer", transport=httpx.MockTransport(handler))
    r.run(TaskInvocation("r1", "t", "", 1, "pass\n", {}, {}, 60), ListRecorder().task_log("t", 1), Cancellation())
    r.run(TaskInvocation("r1", "t", "", 1, "pass\n", {}, {}, 60, env={"K": VALUE}), ListRecorder().task_log("t", 1), Cancellation())
    assert "env" not in seen[0] and seen[1]["env"] == {"K": VALUE}


def test_no_secret_value_is_ever_written_to_a_kubernetes_object():
    from .test_kubejob import FakeCluster, Lines, make_runner

    cluster = FakeCluster()
    try:
        src = "import os\ndef run(ctx):\n    return os.environ['FMP_API_KEY'] == " + repr(VALUE) + "\n"
        out = make_runner(cluster).run(TaskInvocation("run-1", "t", "", 1, src, {}, {}, 60, env={"FMP_API_KEY": VALUE}), Lines(), Cancellation())
        assert out is True  # it reached the task, over the connection to its pod
        assert cluster.bodies and not any(VALUE.encode() in b for b in cluster.bodies)  # not the Job, its pip container or its Secret
        assert not any(VALUE in str(q.url) for q in cluster.requests)
    finally:
        cluster.close()
