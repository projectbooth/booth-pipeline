"""ctx.storage / ctx.catalog inside a real task subprocess, talking to a fake booth-storage and
booth-catalog over real HTTP. Covers the request shapes the platform expects, the token being read
from a file (so it can be refreshed while a long task runs), and the no-access failure mode."""

from __future__ import annotations

import threading
import time

from booth_pipeline.runners.base import Cancellation, TaskAccess, TaskFailed, TaskInvocation
from booth_pipeline.runners.subprocess_runner import SubprocessRunner

from .servers import FakeHttp


class Lines:
    def __init__(self) -> None:
        self.out: list[tuple[str, str]] = []

    def line(self, stream: str, message: str) -> None:
        self.out.append((stream, message))

    @property
    def stdout(self) -> list[str]:
        return [m for s, m in self.out if s == "stdout"]


def invoke(source: str, access: TaskAccess | None, run_id="r1", key="t", timeout=60):
    lines = Lines()
    inv = TaskInvocation(run_id, key, "transform", 1, source, {}, {}, timeout, access)
    out = SubprocessRunner().run(inv, lines, Cancellation())
    return out, lines


def platform_access(fake: FakeHttp, token="tok-1") -> TaskAccess:
    return TaskAccess("acme", token, fake.url + "/modules/storage", fake.url + "/modules/catalog")


def scripted(fake: FakeHttp) -> None:
    def handler(req):
        p = req.path
        if p.endswith("/forbidden"):
            return 403, {"error": "your role in this workspace is read-only"}
        if req.method == "GET" and p.startswith("/modules/storage/api/backends/b1/objects/in/a.txt"):
            return 200, b"hello from storage"
        if req.method == "PUT" and p.startswith("/modules/storage/api/backends/b1/objects/out/"):
            return 200, {"path": p.rsplit("/objects/", 1)[1], "size": len(req.body)}
        if req.method == "GET" and p.startswith("/modules/storage/api/backends/b1/objects"):
            return 200, {"entries": [{"path": "in/a.txt"}]}
        if req.method == "GET" and p == "/modules/storage/api/backends":
            return 200, {"items": [{"id": "b1"}]}
        if req.method == "POST" and p == "/modules/catalog/api/datasets":
            return 201, {"id": "d-1", **req.json()}
        if req.method == "GET" and p.startswith("/modules/catalog/api/datasets"):
            return 200, {"items": [], "total": 0}
        return 404, {"error": "not found"}

    fake.handler = handler


SRC = """
def run(ctx):
    print(ctx.storage.backends()["items"][0]["id"])
    print(ctx.storage.read_text("b1", "in/a.txt"))
    info = ctx.storage.write("b1", "out/result file.csv", "id\\n1\\n", "text/csv")
    print(info["path"], info["size"])
    ds = ctx.catalog.register_dataset("results", "b1", "out/result file.csv", description="d", tags=["x"])
    print(ds["id"], ds["location"])
    return "done"
"""


def test_storage_and_catalog_calls_carry_the_tasks_token_and_workspace():
    with FakeHttp() as fake:
        scripted(fake)
        out, lines = invoke(SRC, platform_access(fake))
        assert out == "done"
        assert lines.stdout == ["b1", "hello from storage", "out/result%20file.csv 5", "d-1 {'backendId': 'b1', 'path': 'out/result file.csv'}"]
        for req in fake.requests:
            assert req.headers["authorization"] == "Bearer tok-1"
            # through the gateway (this base URL is a /modules/ path) only X-Workspace is sent; the gateway
            # sets X-Booth-Workspace itself (see test_gateway_default.py for the direct-URL escape hatch)
            assert req.headers["x-workspace"] == "acme" and "x-booth-workspace" not in req.headers
        put = next(r for r in fake.requests if r.method == "PUT")
        assert put.body == b"id\n1\n" and put.headers["content-type"] == "text/csv"
        post = next(r for r in fake.requests if r.method == "POST")
        assert post.json()["location"] == {"backendId": "b1", "path": "out/result file.csv"} and post.json()["tags"] == ["x"]


def test_the_location_uses_the_backend_id_path_pair_and_never_a_uri():
    """ADR 0045: a location is {backendId, path}, resolved only through booth-storage's own API."""
    with FakeHttp() as fake:
        scripted(fake)
        invoke('def run(ctx):\n    ctx.catalog.register_dataset("d", "b1", "p/q")\n', platform_access(fake))
        body = next(r for r in fake.requests if r.method == "POST").json()
        assert set(body["location"]) == {"backendId", "path"}


def test_a_platform_error_reaches_the_task_as_a_clear_exception_and_fails_it():
    with FakeHttp() as fake:
        scripted(fake)
        src = "def run(ctx):\n    ctx.storage.read('b1', 'forbidden')\n"
        lines = Lines()
        try:
            SubprocessRunner().run(TaskInvocation("r", "t", "sink", 1, src, {}, {}, 60, platform_access(fake)), lines, Cancellation())
        except TaskFailed:
            pass
        else:
            raise AssertionError("the task should have failed")
        text = "\n".join(m for _, m in lines.out)
        assert "PlatformError: HTTP 403: your role in this workspace is read-only" in text


def test_the_token_is_re_read_on_every_call_so_a_running_task_picks_up_a_refresh():
    src = """
import time
def run(ctx):
    ctx.storage.backends()
    time.sleep(3)
    ctx.storage.backends()
"""
    with FakeHttp() as fake:
        scripted(fake)
        runner = SubprocessRunner()
        lines = Lines()
        inv = TaskInvocation("r-9", "slow", "transform", 1, src, {}, {}, 60, platform_access(fake, "tok-OLD"))
        t = threading.Thread(target=lambda: runner.run(inv, lines, Cancellation()))
        t.start()
        for _ in range(100):  # wait for the first call to land
            if fake.requests:
                break
            time.sleep(0.05)
        assert runner.update_token("r-9", "slow", "tok-NEW") is True  # what the runner service does on a push
        t.join(30)
        assert [r.headers["authorization"] for r in fake.requests] == ["Bearer tok-OLD", "Bearer tok-NEW"]
        assert runner.update_token("r-9", "slow", "late") is False  # the task is gone; nothing to update


def test_the_token_lives_in_a_private_file_not_in_the_environment_or_the_payload():
    src = """
import os, stat
def run(ctx):
    path = os.environ["BOOTH_TOKEN_FILE"]
    print("mode", oct(stat.S_IMODE(os.stat(path).st_mode)) if os.name == "posix" else "n/a")
    print("token-in-env", any("tok-SECRET" in v for v in os.environ.values()))
    print("token-in-inputs", "tok-SECRET" in repr(ctx.inputs) + repr(ctx.params))
"""
    with FakeHttp() as fake:
        _, lines = invoke(src, platform_access(fake, "tok-SECRET"))
        assert "token-in-env False" in lines.stdout and "token-in-inputs False" in lines.stdout
        assert any(m in ("mode 0o600", "mode n/a") for m in lines.stdout)


def test_a_task_without_platform_access_fails_with_the_fix_not_a_cryptic_error():
    src = "def run(ctx):\n    ctx.storage.list('b1')\n"
    lines = Lines()
    try:
        SubprocessRunner().run(TaskInvocation("r", "t", "sink", 1, src, {}, {}, 60, None), lines, Cancellation())
    except TaskFailed:
        pass
    text = "\n".join(m for _, m in lines.out)
    assert "ctx.storage is not available: this task has no platform access" in text and "Platform access" in text


def test_an_unreachable_platform_is_a_platform_error_not_a_hang():
    access = TaskAccess("acme", "tok", "http://127.0.0.1:1/modules/storage", "http://127.0.0.1:1/modules/catalog")
    lines = Lines()
    t0 = time.monotonic()
    try:
        SubprocessRunner().run(TaskInvocation("r", "t", "sink", 1, "def run(ctx):\n    ctx.storage.backends()\n", {}, {}, 60, access), lines, Cancellation())
    except TaskFailed:
        pass
    assert time.monotonic() - t0 < 30
    assert "could not reach the platform" in "\n".join(m for _, m in lines.out)
