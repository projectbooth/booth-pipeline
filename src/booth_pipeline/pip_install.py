"""Install one task's Python packages: the task pod's ``pip`` init container (ADR 0115).

Design: docs/design-task-requirements.md. This runs BEFORE the task's own container and before the
credential sidecars, in a container that mounts nothing but the empty ``site`` volume: no runner
bearer, no platform token, no ServiceAccount token. pip may build an sdist, which runs arbitrary
code; that is why it runs here, and why nothing here may ever hold a credential.

Standard library only, like the harness: it must not depend on anything a requirement could
replace.

It installs into ``BOOTH_PIP_TARGET`` (the ``site`` volume) and leaves two files there for the
runner service in the task container: ``.booth-pip.log`` (pip's whole output, copied into the
task's log) and ``.booth-pip.json`` (how long it took and how much it installed). On failure it
writes a short reason as the container's termination message instead, which the API/scheduler pod
reads from the pod's status, and exits 1, which fails the pod and so the task attempt.
"""

from __future__ import annotations

import collections
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

LOG_FILE = ".booth-pip.log"
RECORD_FILE = ".booth-pip.json"
WORK_DIR = ".work"  # pip's HOME and TMPDIR: the root filesystem is read-only. Removed afterwards.

# Kubernetes keeps at most 4096 bytes of a termination message; stay well under it.
MESSAGE_LINES = 20
MESSAGE_BYTES = 2500

# What pip (urllib3 underneath) prints when it never got an answer from the index. Only these make
# a failure "could not reach the index": a package or version that doesn't exist is never blamed on
# the network.
_UNREACHABLE = (
    "newconnectionerror",
    "connecttimeouterror",
    "failed to establish a new connection",
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname provided",
    "network is unreachable",
    "no route to host",
    "connection refused",
    "read timed out",
    "connection timed out",
    "remotedisconnected",
    "connection reset by peer",
)

_QUANTITY = re.compile(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T|k)?$")
_UNITS = {None: 1, "k": 1000, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}


# How pip is started: this image's own interpreter's pip. Tests replace it with a stand-in.
PIP_COMMAND: list[str] | None = None


class InstallFailed(Exception):
    """The install did not produce the packages; the message is what the task's log will show."""


def parse_quantity(q: str) -> int:
    """A Kubernetes storage quantity (``1Gi``, ``500M``, ``1048576``) in bytes."""
    m = _QUANTITY.match(q.strip())
    if not m:
        raise ValueError(f"not a storage quantity: {q!r}")
    return int(float(m.group(1)) * _UNITS[m.group(2)])


def human_bytes(n: int) -> str:
    for unit, size in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"


def tail_message(lines: list[str], limit_lines: int = MESSAGE_LINES, limit_bytes: int = MESSAGE_BYTES) -> str:
    """The last lines of pip's output that fit a termination message, newest kept."""
    kept: list[str] = []
    size = 0
    for line in reversed([ln for ln in lines if ln.strip()][-limit_lines:]):
        b = len(line.encode("utf-8")) + 1
        if size + b > limit_bytes:
            break
        kept.append(line)
        size += b
    return "\n".join(reversed(kept))


def index_host(index_url: str) -> str:
    return urlsplit(index_url).hostname or index_url if index_url else "pypi.org"


def failure_message(output: list[str], index_url: str) -> str:
    """Why pip failed, for the task's log: unreachable index, or pip's own last words."""
    tail = tail_message(output)
    text = "\n".join(output).lower()
    if any(s in text for s in _UNREACHABLE):
        return f"pip install could not reach the package index at {index_host(index_url)}:\n{tail}"
    return f"pip install failed:\n{tail}"


def used_bytes(path: Path) -> int:
    """Disk use as the kubelet counts it for an emptyDir's sizeLimit: allocated blocks."""
    total = 0
    for root, dirs, files in os.walk(path):
        for name in files + dirs:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            total += st.st_blocks * 512 if hasattr(st, "st_blocks") else st.st_size
    return total


def _empty(path: Path) -> None:
    """A rerun finds the previous attempt's files; start from nothing."""
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _kill(proc: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)  # pip and any sdist build it started
        else:  # pragma: no cover - local Windows runs only
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


def install(
    requirements: list[str],
    target: Path,
    *,
    timeout_seconds: float,
    size_limit: int,
    index_url: str = "",
    trusted_host: str = "",
    pip_command: list[str] | None = None,
    out: object = None,
) -> dict[str, object]:
    """Run pip into ``target``; raise InstallFailed with the task-facing reason. Returns the record."""
    out = out or sys.stdout
    _empty(target)
    work = target / WORK_DIR
    work.mkdir()
    req_file = work / "requirements.txt"
    req_file.write_text("".join(r + "\n" for r in requirements), encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(work),
        "TMPDIR": str(work),
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
    }
    if index_url:
        env["PIP_INDEX_URL"] = index_url
    if trusted_host:
        env["PIP_TRUSTED_HOST"] = trusted_host
    cmd = [
        *(pip_command or PIP_COMMAND or [sys.executable, "-m", "pip"]), "install",
        "--no-cache-dir", "--target", str(target), "--prefer-binary",
        "--disable-pip-version-check", "--no-input", "--progress-bar", "off",
        "--timeout", "20", "--retries", "2",
        "-r", str(req_file),
    ]  # fmt: skip
    started = time.monotonic()
    output: collections.deque[str] = collections.deque(maxlen=2000)
    with open(target / LOG_FILE, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd, env=env, cwd=str(work), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )  # fmt: skip

        def pump() -> None:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                output.append(line)
                log.write(line + "\n")
                log.flush()
                print(line, file=out, flush=True)  # type: ignore[call-arg]

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            # pip's own --timeout is per network read: a server trickling bytes never trips it.
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill(proc)
            proc.wait()
            reader.join(timeout=5)
            raise InstallFailed(f"pip install timed out after {timeout_seconds:g}s (runner.pip.timeoutSeconds)") from None
        reader.join(timeout=5)
    shutil.rmtree(work, ignore_errors=True)
    if proc.returncode != 0:
        raise InstallFailed(failure_message(list(output), index_url))
    used = used_bytes(target)
    if used > size_limit:
        raise InstallFailed(f"pip install used {human_bytes(used)}, more than runner.pip.sizeLimit ({human_bytes(size_limit)})")
    record = {"packages": len(requirements), "seconds": round(time.monotonic() - started, 1), "bytes": used}
    (target / RECORD_FILE).write_text(json.dumps(record), encoding="utf-8")
    return record


def main() -> int:
    requirements = [r for r in os.environ.get("BOOTH_PIP_REQUIREMENTS", "").split("\n") if r.strip()]
    target = Path(os.environ.get("BOOTH_PIP_TARGET", "/opt/booth/site"))
    termination_log = os.environ.get("BOOTH_PIP_TERMINATION_LOG", "/dev/termination-log")
    try:
        if not requirements:
            raise InstallFailed("pip install: no requirements were given to the installer")
        record = install(
            requirements,
            target,
            timeout_seconds=float(os.environ.get("BOOTH_PIP_TIMEOUT_SECONDS", "300")),
            size_limit=parse_quantity(os.environ.get("BOOTH_PIP_SIZE_LIMIT", "1Gi")),
            index_url=os.environ.get("PIP_INDEX_URL", ""),
            trusted_host=os.environ.get("PIP_TRUSTED_HOST", ""),
        )
    except (InstallFailed, OSError, ValueError) as e:
        # OSError/ValueError: the installer itself could not work (an unwritable volume, a bad
        # setting). Still a clear reason in the task's log, never a bare traceback.
        message = str(e) if isinstance(e, InstallFailed) else f"pip install could not run: {e}"
        print(message, flush=True)
        try:
            with open(termination_log, "w", encoding="utf-8") as f:
                f.write(message.encode("utf-8")[:4000].decode("utf-8", errors="ignore"))
        except OSError:
            pass
        return 1
    print(f"installed {record['packages']} requirement(s) in {record['seconds']}s ({human_bytes(int(record['bytes']))})", flush=True)  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    sys.exit(main())
