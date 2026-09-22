"""The separate runner pod (ADR 0057) over a real socket: the runner service, and the RemoteRunner
the API/scheduler pod uses to drive it."""

from __future__ import annotations

import threading
import time

import httpx
import pytest

from booth_pipeline.runner_service import create_runner_app
from booth_pipeline.runners.base import Cancellation, TaskAccess, TaskCanceled, TaskFailed, TaskInvocation
from booth_pipeline.runners.remote import RemoteRunner
from booth_pipeline.runners.subprocess_runner import SubprocessRunner

from .servers import serve_asgi

SECRET = "runner-shared-secret"


class Lines:
    def __init__(self) -> None:
        self.out: list[tuple[str, str]] = []

    def line(self, stream: str, message: str) -> None:
        self.out.append((stream, message))

    @property
    def stdout(self) -> list[str]:
        return [m for s, m in self.out if s == "stdout"]


def inv(source: str, run_id="r1", key="t", timeout=60, access: TaskAccess | None = None, inputs=None, params=None) -> TaskInvocation:
    return TaskInvocation(run_id, key, "transform", 1, source, params or {}, inputs or {}, timeout, access)


@pytest.fixture()
def runner_url():
    with serve_asgi(create_runner_app(SECRET, SubprocessRunner(), max_concurrent=2)) as url:
        yield url


def remote(url: str, **kw) -> RemoteRunner:
    return RemoteRunner(url, kw.pop("secret", SECRET), **kw)


def test_a_task_runs_remotely_streams_its_logs_and_returns_its_output(runner_url):
    lines = Lines()
    out = remote(runner_url).run(
        inv("def run(ctx):\n    print('hello', ctx.params['n'], ctx.inputs['up'])\n    return {'ok': True}\n", inputs={"up": [1, 2]}, params={"n": 7}),
        lines,
        Cancellation(),
    )
    assert out == {"ok": True} and lines.stdout == ["hello 7 [1, 2]"]


def test_stdout_and_stderr_stay_on_their_own_streams(runner_url):
    lines = Lines()
    remote(runner_url).run(inv("import sys\nprint('o')\nprint('e', file=sys.stderr)\n"), lines, Cancellation())
    assert {("stdout", "o"), ("stderr", "e")} <= set(lines.out)


def test_a_failing_task_raises_task_failed_with_its_reason_and_traceback_in_the_log(runner_url):
    lines = Lines()
    with pytest.raises(TaskFailed, match="exited with code 1"):
        remote(runner_url).run(inv("raise RuntimeError('kaboom')\n"), lines, Cancellation())
    assert any("RuntimeError: kaboom" in m for _, m in lines.out)


def test_the_runners_own_timeout_is_a_failure(runner_url):
    with pytest.raises(TaskFailed, match="timed out after 1s"):
        remote(runner_url).run(inv("import time\ntime.sleep(30)\n", timeout=1), Lines(), Cancellation())


def test_the_runner_refuses_callers_without_the_shared_secret(runner_url):
    with pytest.raises(TaskFailed, match="refused this module's credentials"):
        remote(runner_url, secret="wrong").run(inv("pass\n"), Lines(), Cancellation())
    assert httpx.post(runner_url + "/v1/run", json={}).status_code == 401  # no header at all
    assert httpx.put(runner_url + "/v1/run/r/t/token", json={"token": "x"}).status_code == 401
    assert httpx.get(runner_url + "/healthz").status_code == 200  # liveness needs no secret


def test_a_runner_cannot_be_constructed_without_a_secret():
    """An open runner would execute arbitrary code for anyone who can reach it."""
    with pytest.raises(ValueError, match="non-empty shared secret"):
        create_runner_app("", SubprocessRunner())


def test_authentication_comes_before_the_body_is_even_looked_at(runner_url):
    # an unauthenticated caller with a malformed body learns nothing about the schema
    assert httpx.post(runner_url + "/v1/run", json={"nonsense": True}).status_code == 401
    assert httpx.post(runner_url + "/v1/run", content=b"not json at all").status_code == 401


def test_hanging_up_cancels_the_task_and_kills_it(runner_url):
    """Cancellation is 'the client closes the connection'; the runner must stop the process."""
    marker = "cancel-marker-file"
    src = f"import time, os\nprint('started', flush=True)\ntime.sleep(60)\nopen({marker!r}, 'w').write('survived')\n"
    cancel, lines = Cancellation(), Lines()
    box: dict = {}

    def go():
        try:
            remote(runner_url).run(inv(src), lines, cancel)
        except BaseException as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=go)
    t.start()
    for _ in range(200):
        if "started" in lines.stdout:
            break
        time.sleep(0.05)
    t0 = time.monotonic()
    cancel.cancel()
    t.join(20)
    assert isinstance(box.get("err"), TaskCanceled) and time.monotonic() - t0 < 10
    # the slot was released: with max_concurrent=2 a further pair of tasks can still run
    for _ in range(2):
        remote(runner_url).run(inv("print('after')\n"), Lines(), Cancellation())


def test_the_runner_reports_capacity_and_recovers(runner_url):
    slow = "import time\nprint('up', flush=True)\ntime.sleep(4)\n"
    started = [Lines(), Lines()]
    threads = [threading.Thread(target=lambda ln=ln: remote(runner_url).run(inv(slow, key=f"k{id(ln)}"), ln, Cancellation())) for ln in started]
    [t.start() for t in threads]
    for _ in range(200):
        if all("up" in s.stdout for s in started):
            break
        time.sleep(0.05)
    with pytest.raises(TaskFailed, match="at capacity"):  # a third while two are running
        remote(runner_url).run(inv("pass\n"), Lines(), Cancellation())
    [t.join(30) for t in threads]
    remote(runner_url).run(inv("pass\n"), Lines(), Cancellation())  # and it is fine again


def test_a_dead_runner_is_a_retryable_failure_not_a_hang():
    with pytest.raises(TaskFailed, match="lost connection to the runner"):
        remote("http://127.0.0.1:1").run(inv("pass\n"), Lines(), Cancellation())


def test_refreshed_tokens_are_pushed_to_the_running_task():
    """A token lives ~10 min and a task can run for hours: the trusted pod keeps minting and pushing."""
    tokens = iter(f"tok-{i}" for i in range(2, 100))
    access = TaskAccess("acme", "tok-1", "http://x", "http://x", refresh=lambda: next(tokens))
    src = (
        "import os, time\n"
        "def run(ctx):\n"
        "    seen = []\n"
        "    for _ in range(14):\n"
        "        tok = open(os.environ['BOOTH_TOKEN_FILE']).read()\n"
        "        if not seen or seen[-1] != tok:\n"
        "            seen.append(tok); print('token', tok, flush=True)\n"
        "        time.sleep(0.25)\n"
    )
    with serve_asgi(create_runner_app(SECRET, SubprocessRunner())) as url:
        lines = Lines()
        remote(url, refresh_seconds=0.6).run(inv(src, access=access), lines, Cancellation())
    seen = [m.split()[1] for m in lines.stdout if m.startswith("token ")]
    assert seen[0] == "tok-1" and len(seen) >= 3 and seen[1] == "tok-2"  # it moved on while the task ran


def test_a_failed_refresh_is_reported_once_and_does_not_kill_the_task():
    def broken():
        raise RuntimeError("core is down")

    access = TaskAccess("acme", "tok-1", "http://x", "http://x", refresh=broken)
    with serve_asgi(create_runner_app(SECRET, SubprocessRunner())) as url:
        lines = Lines()
        out = remote(url, refresh_seconds=0.3).run(inv("import time\ndef run(ctx):\n    time.sleep(2)\n    return 'survived'\n", access=access), lines, Cancellation())
    assert out == "survived"
    warnings = [m for s, m in lines.out if s == "system" and "could not refresh" in m]
    assert len(warnings) == 1 and "core is down" in warnings[0]


def test_a_task_with_no_access_gets_no_token_file():
    with serve_asgi(create_runner_app(SECRET, SubprocessRunner())) as url:
        lines = Lines()
        remote(url).run(inv("import os\nprint('BOOTH_TOKEN_FILE' in os.environ)\n"), lines, Cancellation())
    assert lines.stdout == ["False"]
