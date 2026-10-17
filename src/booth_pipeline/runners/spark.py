"""The Spark runner (ADR 0119; design: docs/design-spark-runner.md, accepted with rulings).

A Spark task still gets its own Job pod (ADR 0096), but that pod runs no user code: the module's
Spark client (``_spark_client.py``) submits the task's snapshotted PySpark code to booth-spark through
core's gateway, as the run, and polls it. This class is the API/scheduler-pod half. It:

* gives the pod's client where booth-spark is and how long to wait for capacity;
* turns the client's events into the run's log lines and the attempt's Spark record, and the driver
  log into the ``spark`` stream (shown to editors and owners only, ruling 4);
* stops the application itself on cancel, timeout or failure, since the pod may be killed outright,
  and before a retry makes sure the previous attempt's application has ended, so two never write at
  once (section 5);
* asks the engine to wait at least ``retry_min_delay_seconds`` before retrying an attempt whose
  application reached ``running`` (SeaweedFS keeps an emptied folder about 2 minutes, so an
  ``errorifexists`` write retried sooner fails spuriously);
* says, per workspace, whether booth-spark is there at all (``GET /modules/spark/v1/limits`` as the
  caller, cached briefly).
"""

from __future__ import annotations

import collections
import dataclasses
import json
import logging
import threading
import time
from typing import Any

import httpx

from .base import Cancellation, Runner, TaskAccess, TaskCanceled, TaskFailed, TaskInvocation, TaskLog

log = logging.getLogger(__name__)

EVENT = "BOOTH-SPARK-EVENT "  # must match _spark_client.EVENT
TERMINAL = ("succeeded", "failed", "stopped")
SPARK_STREAM = "spark"  # the driver log's lines in the run log


class SparkTaskFailed(TaskFailed):
    """A Spark attempt failed. ``retry_not_before`` is the least wait before a retry (0: none)."""

    def __init__(self, message: str, retry_not_before: float = 0) -> None:
        super().__init__(message)
        self.retry_not_before = retry_not_before


class SparkApi:
    """booth-spark's ``/v1`` through core's gateway, from the API/scheduler pod (as the run, or as the
    caller for the availability check)."""

    def __init__(self, core_url: str, transport: httpx.BaseTransport | None = None, timeout: float = 10.0) -> None:
        self.base = core_url.rstrip("/") + "/modules/spark"
        self._http = httpx.Client(transport=transport, timeout=timeout)

    def _h(self, token: str, workspace: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "X-Workspace": workspace}

    def limits(self, token: str, workspace: str) -> httpx.Response:
        return self._http.get(f"{self.base}/v1/limits", headers=self._h(token, workspace))

    def get(self, app_id: str, token: str, workspace: str) -> dict[str, Any] | None:
        r = self._http.get(f"{self.base}/v1/applications/{app_id}", headers=self._h(token, workspace))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def stop(self, app_id: str, token: str, workspace: str) -> None:
        r = self._http.post(f"{self.base}/v1/applications/{app_id}/stop", headers=self._h(token, workspace))
        if r.status_code not in (200, 202, 404):
            r.raise_for_status()


class SparkRunner:
    id = "spark"

    def __init__(
        self,
        jobs: Runner,
        core_url: str,
        *,
        poll_seconds: float = 5,
        admission_wait_seconds: float = 900,
        retry_min_delay_seconds: float = 150,
        stop_wait_seconds: float = 60,
        availability_ttl_seconds: float = 60,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._jobs = jobs
        self._core_url = core_url.rstrip("/")
        self._api = SparkApi(core_url, transport)
        self._poll = poll_seconds
        self._admission_wait = admission_wait_seconds
        self.retry_min_delay_seconds = retry_min_delay_seconds
        self._stop_wait = stop_wait_seconds
        self._ttl = availability_ttl_seconds
        self._available: dict[str, tuple[float, str | None]] = {}
        # (run id, task key) -> the latest attempt's application, so the next attempt can make sure it
        # has ended. In memory: a retry happens in the same process as the attempt before it.
        self._apps: collections.OrderedDict[tuple[str, str], str] = collections.OrderedDict()
        self._lock = threading.Lock()

    # ---- whether booth-spark is there ----
    def availability(self, token: str, workspace: str) -> str | None:
        """None if a Spark task can run in this workspace, else why not. Cached per workspace."""
        now = time.monotonic()
        with self._lock:
            hit = self._available.get(workspace)
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        try:
            r = self._api.limits(token, workspace)
            if r.status_code == 200:
                reason = None
            elif r.status_code == 404:
                reason = "booth-spark is not installed (core's gateway has no /modules/spark)"
            else:
                reason = f"booth-spark answered HTTP {r.status_code}"
        except httpx.HTTPError as e:
            reason = f"booth-spark could not be reached ({e.__class__.__name__})"
        with self._lock:
            self._available[workspace] = (now, reason)
        return reason

    # ---- one attempt ----
    def run(self, inv: TaskInvocation, log_: TaskLog, cancel: Cancellation) -> Any:
        if inv.access is None:
            raise TaskFailed("a Spark task is submitted as the run, and this run has no token")
        access = inv.access
        key = (inv.run_id, inv.task_key)
        with self._lock:
            previous = self._apps.pop(key, None)
        if previous:
            self._ensure_ended(previous, access, log_)
        options = dict(inv.spark or {})
        spark = {
            **options,
            "url": f"{self._core_url}/modules/spark",
            "workspace": access.workspace,
            "pollSeconds": self._poll,
            "admissionWaitSeconds": self._admission_wait,
        }
        tracker = _Tracker(log_, summary(options, inv.timeout_seconds))
        try:
            output = self._jobs.run(dataclasses.replace(inv, spark=spark), tracker, cancel)
        except TaskCanceled:
            self._stop(tracker, access, log_, "the task was canceled")
            raise
        except TaskFailed as e:
            self._stop(tracker, access, log_, "the attempt failed")
            self._remember(key, tracker.app_id)
            floor = self.retry_min_delay_seconds if tracker.reached_running else 0
            raise SparkTaskFailed(str(e), retry_not_before=floor) from None
        except Exception:
            self._stop(tracker, access, log_, "the runner failed")
            self._remember(key, tracker.app_id)
            raise
        return output

    def _remember(self, key: tuple[str, str], app_id: str | None) -> None:
        if not app_id:
            return
        with self._lock:
            self._apps[key] = app_id
            while len(self._apps) > 1000:
                self._apps.popitem(last=False)

    def _fresh_token(self, access: TaskAccess) -> str:
        try:
            return access.refresh() if access.refresh else access.token
        except Exception as e:  # noqa: BLE001 - a refused refresh still leaves the token we hold
            log.warning("could not refresh the run's token to manage its Spark application: %s", e)
            return access.token

    def _stop(self, tracker: _Tracker, access: TaskAccess, log_: TaskLog, why: str) -> None:
        if not tracker.app_id or tracker.state in TERMINAL:
            return
        try:
            self._api.stop(tracker.app_id, self._fresh_token(access), access.workspace)
            log_.line("system", f"spark: asked booth-spark to stop application {tracker.app_id} ({why})")
        except Exception as e:  # noqa: BLE001 - Spark's maxDuration still ends it
            log_.line("system", f"spark: could not stop application {tracker.app_id} ({e.__class__.__name__}); its maxDuration still ends it")

    def _ensure_ended(self, app_id: str, access: TaskAccess, log_: TaskLog) -> None:
        """The previous attempt's application must have ended before another starts (section 5)."""
        token = self._fresh_token(access)
        try:
            app = self._api.get(app_id, token, access.workspace)
            if app is None or app.get("state") in TERMINAL:
                return
            log_.line("system", f"spark: the previous attempt's application {app_id} is still {app.get('state')}; stopping it before retrying")
            self._api.stop(app_id, token, access.workspace)
            deadline = time.monotonic() + self._stop_wait
            while time.monotonic() < deadline:
                time.sleep(min(2.0, self._stop_wait))
                app = self._api.get(app_id, token, access.workspace)
                if app is None or app.get("state") in TERMINAL:
                    log_.line("system", f"spark: application {app_id} has ended ({(app or {}).get('state', 'gone')})")
                    return
        except httpx.HTTPError as e:
            raise TaskFailed(f"could not check that the previous Spark application {app_id} has ended ({e.__class__.__name__}); not starting another") from None
        raise TaskFailed(f"the previous Spark application {app_id} is still running after {self._stop_wait:g}s; not starting another")


class _Tracker:
    """Between the Job runner and the task's log: turns the pod's events into system lines and the
    attempt's Spark record, and the driver log (the client's stdout) into the ``spark`` stream."""

    def __init__(self, log_: TaskLog, summary: str) -> None:
        self._log = log_
        self._summary = summary
        self.app_id: str | None = None
        self.state: str | None = None
        self.reached_running = False

    def line(self, stream: str, message: str) -> None:
        if stream == "stderr" and message.startswith(EVENT):
            try:
                self._event(json.loads(message[len(EVENT) :]))
                return
            except (ValueError, TypeError, KeyError):
                pass
        self._log.line(SPARK_STREAM if stream == "stdout" else stream, message)

    def _event(self, ev: dict[str, Any]) -> None:
        kind = ev["event"]
        if kind == "submitted":
            self.app_id, self.state = str(ev["id"]), str(ev.get("state", "pending"))
            self._log.line("system", f"spark: submitted application {self.app_id} ({self._summary})")
        elif kind == "state":
            before, self.state = self.state, str(ev["state"])
            reason = ev.get("reason") or ""
            self.reached_running = self.reached_running or self.state in ("running", "succeeded")
            self._log.line("system", f"spark: {before} → {self.state}" + (f": {reason}" if reason else ""))
        elif kind == "waiting":
            self._log.line("system", f"spark: waiting for capacity ({ev.get('reason', '')}); retrying in {ev.get('retryIn', 30)}s")
            return
        else:
            return
        record = getattr(self._log, "external", None)
        if callable(record):
            record({"kind": "spark", "id": self.app_id, "state": self.state, "reason": ev.get("reason") or None})


def summary(options: dict[str, Any], timeout_seconds: int) -> str:
    """What was asked for, in one line: driver 1g, 1–2 executors × 2g; data: lakehouse, storage b:p rw."""
    res = options.get("resources") or {}
    drv = (res.get("driver") or {}).get("memory")
    ex = res.get("executors") or {}
    parts = []
    if drv:
        parts.append(f"driver {drv}")
    if ex.get("min") is not None or ex.get("max") is not None or ex.get("memory"):
        span = f"{ex.get('min', '?')}–{ex.get('max', '?')}" if ex.get("min") != ex.get("max") else f"{ex.get('min')}"
        parts.append(f"{span} executors" + (f" × {ex['memory']}" if ex.get("memory") else ""))
    if not parts:
        parts.append("booth-spark's default resources")
    data = options.get("dataAccess") or {}
    what = [n for n in ("database", "lakehouse") if data.get(n)]
    what += [f"storage {s['backendId']}:{s['path']} {'rw' if s.get('access') == 'readwrite' else 'r'}" for s in data.get("storage") or []]
    return ", ".join(parts) + f"; data: {', '.join(what) if what else 'none'}; at most {timeout_seconds}s"
