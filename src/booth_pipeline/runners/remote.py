"""The remote runner: drives one task in a credential-less task pod over HTTP (ADR 0057/0096).

``KubernetesJobRunner`` uses it against each task's own Job pod once that pod is Ready. It never
executes user code itself. For one task attempt it:

1. POSTs the task snapshot to the runner service and reads the NDJSON stream back, forwarding log
   lines to the run log as they arrive;
2. while the task runs, **pushes a fresh platform token every few minutes** (a token lives ~10
   minutes; a task can run for hours). Minting happens *here*, in the trusted pod — the runner only
   ever receives the resulting short-lived token, never the credential that mints it;
3. cancels by **hanging up**: the runner kills the task when its client disconnects, so cancellation
   needs no separate call that could be lost.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from typing import Any

import httpx

from .base import (
    RUNNER_PROTOCOL,
    Cancellation,
    TaskCanceled,
    TaskFailed,
    TaskInvocation,
    TaskLog,
    TaskSkipped,
)

REBUILD = "rebuild it FROM this release's booth-pipeline image, or remove it from runner.images"

log = logging.getLogger(__name__)

# A token is good for ~10 minutes (ADR 0056); replacing it every 4 leaves two failed attempts of headroom.
DEFAULT_REFRESH_SECONDS = 240.0


def _hang_up(resp: httpx.Response) -> None:
    """Disconnect from the runner NOW, from a thread other than the one reading the stream.

    ``resp.close()`` alone is not enough on Linux (where this runs in production): closing a socket
    from another thread neither wakes a ``recv`` already blocked on it nor sends a FIN while that
    ``recv`` holds it, so the reader stayed blocked — and the runner kept the task running — until
    the runner's next 15 s keep-alive line arrived. ``shutdown(SHUT_RDWR)`` does both at once: the
    blocked read returns immediately and the runner sees the hang-up. Windows happened to behave
    either way, which is why this only ever showed up in CI.
    """
    stream = resp.extensions.get("network_stream")
    sock = stream.get_extra_info("socket") if stream is not None else None
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # already closed or reset: the hang-up has happened either way
    resp.close()


class RemoteRunner:
    id = "base"  # it *is* the base runner, just not in this pod (a task's `runner` is still "base")

    def __init__(
        self,
        url: str,
        secret: str,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        transport: httpx.BaseTransport | None = None,
        image: str = "",
    ) -> None:
        """``image``: set for a pod running an operator's own runner image (runner.images, named so in
        messages). Its runner service may be from another release, so its protocol is checked first."""
        self._image = image
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {secret}"}
        self._refresh = refresh_seconds
        # A read timeout well above the runner's 15s keep-alive: silence for that long means it is gone.
        self._http = httpx.Client(transport=transport, timeout=httpx.Timeout(10.0, read=60.0))

    def close(self) -> None:
        self._http.close()

    def check_protocol(self) -> None:
        """Refuse, with how to fix it, a runner image whose runner service doesn't speak this module's
        protocol revision: an old one would run a skip or a secret wrongly, not fail."""
        what = f"the runner image {self._image!r}" if self._image else "the task's runner"
        try:
            r = self._http.get(f"{self._url}/v1/protocol", headers=self._headers, timeout=10.0)
        except httpx.HTTPError as e:
            raise TaskFailed(f"could not ask {what} which protocol it speaks: {e.__class__.__name__}") from None
        if r.status_code == 404:
            raise TaskFailed(f"{what} is older than this booth-pipeline (its runner service doesn't report its protocol; this release speaks {RUNNER_PROTOCOL}): {REBUILD}")
        if r.status_code != 200:
            raise TaskFailed(f"{what} answered HTTP {r.status_code} when asked its protocol")
        try:
            body = r.json()
            got, version = int(body["protocol"]), str(body.get("version", "?"))
        except (ValueError, KeyError, TypeError):
            raise TaskFailed(f"{what} gave an unreadable answer when asked its protocol") from None
        if got != RUNNER_PROTOCOL:
            age = "older" if got < RUNNER_PROTOCOL else "newer"
            raise TaskFailed(f"{what} is {age} than this booth-pipeline (built from {version}, runner protocol {got}; this release speaks {RUNNER_PROTOCOL}): {REBUILD}")

    def run(self, inv: TaskInvocation, log_: TaskLog, cancel: Cancellation) -> Any:
        if cancel.canceled:
            raise TaskCanceled()
        if self._image:
            self.check_protocol()
        body: dict[str, Any] = {
            "runId": inv.run_id,
            "taskKey": inv.task_key,
            "kind": inv.kind,
            "attempt": inv.attempt,
            "source": inv.source,
            "language": inv.language,  # ADR 0064: without it the far side runs every task as Python
            "params": inv.params,
            "inputs": inv.inputs,
            "timeoutSeconds": inv.timeout_seconds,
            "requirements": list(inv.requirements),  # ADR 0115: installed before; the far side only checks
            "spark": inv.spark,  # ADR 0119: the far side runs the Spark client instead of the task's code
            "trigger": inv.trigger,  # ADR 0123: ctx.trigger
            "access": None
            if inv.access is None
            else {
                "workspace": inv.access.workspace,
                "token": inv.access.token,
                "storageUrl": inv.access.storage_url,
                "catalogUrl": inv.access.catalog_url,
            },
        }
        if inv.env:
            # ADR 0128: the task's secrets, only when it has some, so a task without any still runs on a
            # runner image from before them. Plaintext HTTP inside the cluster, as the platform token
            # already travels; the task NetworkPolicy admits only this pod.
            body["env"] = inv.env
        done = threading.Event()
        stream_box: dict[str, httpx.Response] = {}

        def watch_cancel() -> None:
            while not done.wait(0.2):
                if cancel.canceled:
                    resp = stream_box.get("r")
                    if resp is not None:
                        _hang_up(resp)  # hanging up is the cancel signal the runner acts on
                    return

        threads = [threading.Thread(target=watch_cancel, daemon=True)]
        if inv.access is not None and inv.access.refresh is not None:
            threads.append(threading.Thread(target=self._keep_token_fresh, args=(inv, log_, done), daemon=True))
        for t in threads:
            t.start()
        try:
            return self._stream(body, log_, cancel, stream_box)
        finally:
            done.set()

    def _stream(self, body: dict[str, Any], log_: TaskLog, cancel: Cancellation, box: dict[str, httpx.Response]) -> Any:
        try:
            with self._http.stream("POST", f"{self._url}/v1/run", json=body, headers=self._headers) as resp:
                box["r"] = resp
                if resp.status_code == 429:
                    raise TaskFailed("the runner is at capacity; try again shortly")
                if resp.status_code in (401, 403):
                    raise TaskFailed("the runner refused this module's credentials (an operator must check the runner auth Secret)")
                if resp.status_code != 200:
                    raise TaskFailed(f"the runner returned HTTP {resp.status_code}")
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue  # a torn line is never fatal; the terminal event is what matters
                    kind = ev.get("t")
                    if kind == "log":
                        log_.line(str(ev.get("stream", "stdout")), str(ev.get("message", "")))
                    elif kind == "result":
                        if "skipped" in ev:
                            raise TaskSkipped(str(ev.get("skipped") or "skipped by the task"))
                        return ev.get("output")
                    elif kind == "error":
                        if ev.get("kind") == "canceled":
                            raise TaskCanceled()
                        raise TaskFailed(str(ev.get("message", "task failed")))
        except (httpx.HTTPError, OSError) as e:
            if cancel.canceled:
                raise TaskCanceled() from None
            raise TaskFailed(f"lost connection to the runner: {e.__class__.__name__}") from None
        if cancel.canceled:
            raise TaskCanceled()
        raise TaskFailed("the runner ended the stream without a result")

    def _keep_token_fresh(self, inv: TaskInvocation, log_: TaskLog, done: threading.Event) -> None:
        assert inv.access is not None and inv.access.refresh is not None
        warned = False
        while not done.wait(self._refresh):
            try:
                token = inv.access.refresh()
                r = self._http.put(
                    f"{self._url}/v1/run/{inv.run_id}/{inv.task_key}/token", json={"token": token}, headers=self._headers
                )
                if r.status_code == 404:
                    return  # the task already finished on the runner
                r.raise_for_status()
                warned = False
            except Exception as e:  # noqa: BLE001 - never let a refresh problem kill the task itself
                # Say so once per outage: when the old token lapses the task's own calls will start
                # failing with 401, and this line is the explanation. (Typically the owner's recency
                # window closed mid-run, ADR 0058.)
                if not warned:
                    warned = True
                    log_.line("system", f"could not refresh this task's platform token ({e}); its access ends when the current token expires")
