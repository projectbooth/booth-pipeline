"""The built container image, run exactly as the Helm chart runs it, actually *executing tasks*.

The chart pins a non-root user, a read-only root filesystem, dropped capabilities and a `/tmp`
emptyDir as the only writable path. `/healthz` answering proves the process starts; it does not
prove a task can run, and task execution is precisely what needs to write (a per-task working
directory, Dagster's scratch space). This test runs a real two-task pipeline inside that
lockdown. Needs Docker and the image: ``docker build -t booth-pipeline:dev .``
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

SCRIPT = r'''
import os, sys
from booth_pipeline.engine import execute
from booth_pipeline.model import PipelineSpec
from booth_pipeline.runners.base import Cancellation
from booth_pipeline.runners.registry import RunnerRegistry
from booth_pipeline.runners.subprocess_runner import SubprocessRunner

class Rec:
    def task_started(self, k, a): print("started", k, a)
    def task_attempt_finished(self, k, a, s, e): print("finished", k, s, e)
    def system(self, m): print("system", m)
    def task_log(self, k, a):
        class L:
            def line(self, stream, msg): print(f"[{k}/{stream}] {msg}")
        return L()

spec = PipelineSpec.model_validate({"tasks": [
  {"key": "a", "kind": "source", "code": {"type": "inline", "source":
    "import os\nprint('uid', os.getuid())\nopen('scratch.txt', 'w').write('x')\ndef run(ctx):\n    return 41\n"}},
  {"key": "b", "kind": "sink", "dependsOn": ["a"], "code": {"type": "inline", "source":
    "def run(ctx):\n    print('got', ctx.inputs['a'] + 1)\n    try:\n        open('/etc/nope', 'w')\n    except OSError:\n        print('rootfs read-only')\n"}},
]})
res = execute(spec, run_id="smoke", registry=RunnerRegistry([SubprocessRunner()]), recorder=Rec(), cancel=Cancellation())
sys.exit(0 if res.success else 1)
'''


def image_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "image", "inspect", "booth-pipeline:dev"], capture_output=True).returncode == 0


@pytest.mark.skipif(not image_available(), reason="needs docker and the image (docker build -t booth-pipeline:dev .)")
def test_a_pipeline_executes_inside_the_charts_security_lockdown():
    out = subprocess.run(
        ["docker", "run", "--rm", "-i", "--read-only", "--tmpfs", "/tmp", "--user", "65532:65532", "--cap-drop", "ALL",
         "--security-opt", "no-new-privileges", "--entrypoint", "python", "booth-pipeline:dev", "-"],
        input=SCRIPT, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "[a/stdout] uid 65532" in out.stdout  # really the unprivileged user
    assert "[b/stdout] got 42" in out.stdout  # data flowed between two subprocess tasks
    assert "[b/stdout] rootfs read-only" in out.stdout  # and the lockdown really was in force


@pytest.mark.skipif(not image_available(), reason="needs docker and the image (docker build -t booth-pipeline:dev .)")
def test_the_runner_entrypoint_runs_hardened_authenticates_and_scrubs_its_own_secret():
    """The runner pod's actual command (ADR 0057), under the chart's lockdown: it serves, refuses a
    caller with no bearer, and a task it runs sees neither the runner's own secret nor anything else."""
    import json
    import time
    import urllib.error
    import urllib.request
    import uuid

    name, port = f"bp-runner-{uuid.uuid4().hex[:6]}", 18098
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "--read-only", "--tmpfs", "/tmp", "--user", "65532:65532", "--cap-drop", "ALL",
         "--security-opt", "no-new-privileges", "-e", "BOOTH_RUNNER_AUTH_TOKEN=s3cret", "-p", f"{port}:8080", "--entrypoint", "booth-pipeline-runner", "booth-pipeline:dev"],
        check=True, capture_output=True,
    )
    try:
        base, up = f"http://127.0.0.1:{port}", False
        for _ in range(60):
            try:
                up = urllib.request.urlopen(base + "/healthz", timeout=2).status == 200
                break
            except OSError:
                time.sleep(0.5)
        assert up, subprocess.run(["docker", "logs", name], capture_output=True, text=True).stdout[-1500:]
        try:
            urllib.request.urlopen(urllib.request.Request(base + "/v1/run", data=b"{}", method="POST"), timeout=5)
            raise AssertionError("an unauthenticated call was accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 401
        body = {"runId": "r", "taskKey": "t", "kind": "source", "attempt": 1, "params": {}, "inputs": {}, "timeoutSeconds": 30,
                "source": "import os\nprint('env', sorted(k for k in os.environ if 'BOOTH' in k or 'SECRET' in k.upper()))\ndef run(ctx):\n    return 7\n"}
        req = urllib.request.Request(base + "/v1/run", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Bearer s3cret", "Content-Type": "application/json"})
        events = [json.loads(line) for line in urllib.request.urlopen(req, timeout=30).read().decode().splitlines() if line]
        assert {"t": "result", "output": 7} in events
        env_line = next(e["message"] for e in events if e["t"] == "log")
        assert env_line == "env ['BOOTH_RUN_ID', 'BOOTH_TASK_KEY']"  # the runner's own bearer never reaches a task
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
