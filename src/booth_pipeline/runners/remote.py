"""The remote runner: executes a task in the separate, credential-less runner pod (ADR 0057).

This is the ``Runner`` implementation the API/scheduler pod uses in a real deployment. It never
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
import threading
from typing import Any

import httpx

from .base import Cancellation, TaskCanceled, TaskFailed, TaskInvocation, TaskLog

log = logging.getLogger(__name__)

# A token is good for ~10 minutes (ADR 0056); replacing it every 4 leaves two failed attempts of headroom.
DEFAULT_REFRESH_SECONDS = 240.0


class RemoteRunner:
    id = "base"  # it *is* the base runner, just not in this pod (a task's `runner` is still "base")

    def __init__(
        self,
        url: str,
        secret: str,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {secret}"}
        self._refresh = refresh_seconds
        # A read timeout well above the runner's 15s keep-alive: silence for that long means it is gone.
        self._http = httpx.Client(transport=transport, timeout=httpx.Timeout(10.0, read=60.0))

    def close(self) -> None:
        self._http.close()

    def run(self, inv: TaskInvocation, log_: TaskLog, cancel: Cancellation) -> Any:
        if cancel.canceled:
            raise TaskCanceled()
        body: dict[str, Any] = {
            "runId": inv.run_id,
            "taskKey": inv.task_key,
            "kind": inv.kind,
            "attempt": inv.attempt,
            "source": inv.source,
            "params": inv.params,
            "inputs": inv.inputs,
            "timeoutSeconds": inv.timeout_seconds,
            "access": None
            if inv.access is None
            else {
                "workspace": inv.access.workspace,
                "token": inv.access.token,
                "storageUrl": inv.access.storage_url,
                "catalogUrl": inv.access.catalog_url,
            },
        }
        done = threading.Event()
        stream_box: dict[str, httpx.Response] = {}

        def watch_cancel() -> None:
            while not done.wait(0.2):
                if cancel.canceled:
                    resp = stream_box.get("r")
                    if resp is not None:
                        resp.close()  # hanging up is the cancel signal the runner acts on
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
