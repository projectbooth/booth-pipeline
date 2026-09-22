"""The base runner: the default execution path every install has, needing no other module.

Each task attempt runs in its **own subprocess of the service's own interpreter** (``python -I``)
in a private temp directory, with a scrubbed environment. That gives real isolation of *state* — a
task cannot corrupt the service's memory, hang its threads, or ``sys.exit`` it — plus a hard
timeout and cancellation by killing the process tree. This applies identically no matter what
language the task's source is in: what changes per language is only which file the source is
written to and which harness script the subprocess runs (``runners/languages.py``, ADR 0064).

What it is NOT: a security sandbox. The subprocess is the same OS user in the same pod, so task
code can do anything the module process can. Who may run code, and where, is an open
cross-cutting question — see docs/decisions/0002-execution-isolation.md. Do not weaken the
seam (``Runner``) that lets a stronger runner replace this one.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import languages
from .base import Cancellation, TaskCanceled, TaskFailed, TaskInvocation, TaskLog

TOKEN_FILE = ".booth-platform-token"
MAX_LOG_CHUNK = 8192

# Variables a subprocess needs just to start. Everything else in the service's environment —
# the database DSN, OIDC settings, event-bus credentials — is deliberately NOT passed on.
_BASE_ENV_KEYS = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "LANG", "LC_ALL", "TZ", "PATHEXT", "COMSPEC")


class SubprocessRunner:
    id = "base"

    def __init__(
        self,
        python: str | None = None,
        env_passthrough: tuple[str, ...] = (),
        workdir_root: str | None = None,
    ) -> None:
        self._python = python or sys.executable
        self._passthrough = env_passthrough
        self._workdir_root = workdir_root
        # (run_id, task_key) -> the token file of the attempt currently running, so a fresh token
        # can be swapped in while a long task is still going.
        self._token_files: dict[tuple[str, str], str] = {}
        self._token_lock = threading.Lock()

    def update_token(self, run_id: str, task_key: str, token: str) -> bool:
        """Replace the platform token of a running task (atomically: a reader never sees a half-written
        file). False if that task is not running here."""
        with self._token_lock:
            path = self._token_files.get((run_id, task_key))
        if path is None:
            return False
        _write_token(path, token)
        return True

    def _env(self, inv: TaskInvocation, workdir: str) -> dict[str, str]:
        env = {k: os.environ[k] for k in (*_BASE_ENV_KEYS, *self._passthrough) if k in os.environ}
        if inv.access is not None:
            env["BOOTH_TOKEN_FILE"] = os.path.join(workdir, TOKEN_FILE)
        env.update(
            {
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
                "HOME": workdir,
                "TMPDIR": workdir,
                "TEMP": workdir,
                "TMP": workdir,
                "BOOTH_RUN_ID": inv.run_id,
                "BOOTH_TASK_KEY": inv.task_key,
            }
        )
        return env

    def run(self, inv: TaskInvocation, log: TaskLog, cancel: Cancellation) -> Any:
        if cancel.canceled:
            raise TaskCanceled()
        workdir = tempfile.mkdtemp(prefix="booth-task-", dir=self._workdir_root)
        try:
            return self._run_in(workdir, inv, log, cancel)
        finally:
            with self._token_lock:
                self._token_files.pop((inv.run_id, inv.task_key), None)
            shutil.rmtree(workdir, ignore_errors=True)

    def _run_in(self, workdir: str, inv: TaskInvocation, log: TaskLog, cancel: Cancellation) -> Any:
        handler = languages.get(inv.language)
        Path(workdir, handler.source_filename).write_text(inv.source, encoding="utf-8")
        token_key = (inv.run_id, inv.task_key)
        access_meta = None
        if inv.access is not None:
            # The token goes in a private file, NOT the environment or the payload: it is replaced
            # while the task runs, and a file is what the harness re-reads on every request.
            _write_token(os.path.join(workdir, TOKEN_FILE), inv.access.token)
            access_meta = {"workspace": inv.access.workspace, "storageUrl": inv.access.storage_url, "catalogUrl": inv.access.catalog_url}
            with self._token_lock:
                self._token_files[token_key] = os.path.join(workdir, TOKEN_FILE)
        Path(workdir, "invocation.json").write_text(
            json.dumps(
                {
                    "runId": inv.run_id,
                    "taskKey": inv.task_key,
                    "kind": inv.kind,
                    "attempt": inv.attempt,
                    "params": inv.params,
                    "inputs": inv.inputs,
                    "access": access_meta,
                }
            ),
            encoding="utf-8",
        )
        result_path = os.path.join(workdir, "result.json")

        popen_kwargs: dict[str, Any] = {}
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True  # own process group, so the whole tree can be killed
        else:  # pragma: no cover - exercised on Windows dev machines
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        proc = subprocess.Popen(
            [self._python, "-I", str(handler.harness), workdir, result_path],
            cwd=workdir,
            env=self._env(inv, workdir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )
        readers = [
            threading.Thread(target=_pump, args=(proc.stdout, "stdout", log), daemon=True),
            threading.Thread(target=_pump, args=(proc.stderr, "stderr", log), daemon=True),
        ]
        for t in readers:
            t.start()

        deadline = time.monotonic() + inv.timeout_seconds
        outcome: str | None = None
        while True:
            try:
                proc.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel.canceled:
                outcome = "canceled"
            elif time.monotonic() >= deadline:
                outcome = "timeout"
            if outcome:
                _kill_tree(proc)
                proc.wait()
                break
        for t in readers:
            t.join(timeout=5)

        if outcome == "canceled":
            raise TaskCanceled()
        if outcome == "timeout":
            raise TaskFailed(f"timed out after {inv.timeout_seconds}s")
        if proc.returncode != 0:
            raise TaskFailed(f"task exited with code {proc.returncode}")
        try:
            with open(result_path, encoding="utf-8") as f:
                return json.load(f)["output"]
        except FileNotFoundError:
            raise TaskFailed("task finished without producing a result") from None


def _write_token(path: str, token: str) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    os.replace(tmp, path)  # atomic: the harness re-reads this on every request


def _pump(pipe: Any, stream: str, log: TaskLog) -> None:
    """Forward a pipe to the log a chunk at a time. Bounded reads: a task printing one giant
    line becomes several log lines, never an unbounded buffer."""
    try:
        for raw in iter(lambda: pipe.readline(MAX_LOG_CHUNK), b""):
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            log.line(stream, text)
    finally:
        pipe.close()


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "posix":
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:  # pragma: no cover
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
