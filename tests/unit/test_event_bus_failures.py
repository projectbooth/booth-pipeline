"""ADR 0123 revision 2026-10-22: pipeline's core job never depends on the event bus. In every way the bus can
fail (no credential at all, a credential the bus rejects, the client unable to read the credential, no
stream, the bus going away mid-session) the app starts, the scheduler keeps firing an interval schedule,
the consumer keeps retrying on its own thread, and the event trigger says the bus is unavailable and why.
Runs the real app (scheduler and consumer started) against real NATS servers from
hack/docker-compose.test.yml: one open, one that requires a password."""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from booth_pipeline import event_bus as event_bus_module

from .harness import Env, make_env, task
from .test_event_bus_nats import fresh_stream, nats_url, publish

SEED = b"SUAMLK2ZNL35WSMW37E7UD4VZ7ELPKW7DHC3BWBSD2GCZ7IUQQXZIORRBU"  # a throwaway user nkey, test-only


@pytest.fixture(autouse=True)
def quick_retries(monkeypatch):
    # Look for a credential, and retry a failed session, every second rather than every 15-30 s.
    monkeypatch.setattr(event_bus_module, "IDLE_POLL_SECONDS", 1.0)
    monkeypatch.setattr(event_bus_module, "MAX_BACKOFF_SECONDS", 1.0)
    orig = event_bus_module.EventBus.__init__

    def init(self, *a, **kw):
        kw.setdefault("idle_poll_seconds", 1.0)
        kw.setdefault("max_backoff_seconds", 1.0)
        orig(self, *a, **kw)

    monkeypatch.setattr(event_bus_module.EventBus, "__init__", init)


def auth_url() -> str:
    import os

    url = os.environ.get("BOOTH_TEST_NATS_AUTH_URL")
    if url:
        return url
    if os.environ.get("BOOTH_TEST_REQUIRE_EMULATORS") == "1":
        pytest.fail("BOOTH_TEST_NATS_AUTH_URL is not set but BOOTH_TEST_REQUIRE_EMULATORS=1")
    pytest.skip("BOOTH_TEST_NATS_AUTH_URL not set; start hack/docker-compose.test.yml (CI always does)")


def credential_dir(tmp_path: Path, url: str | None, creds: bool) -> Path:
    """What core's Secret looks like mounted: `url` and `nats.creds` (either may be missing)."""
    import nkeys

    d = tmp_path / "event-bus"
    d.mkdir()
    if url is not None:
        (d / "url").write_text(url)
    if creds:
        seed = nkeys.from_seed(SEED).seed.decode()
        (d / "nats.creds").write_text(
            "-----BEGIN NATS USER JWT-----\neyJ0eXAiOiJKV1QifQ.e30.sig\n------END NATS USER JWT------\n\n"
            f"-----BEGIN USER NKEY SEED-----\n{seed}\n------END USER NKEY SEED------\n"
        )
    return d


def app(bus_dir: Path) -> Env:
    return make_env(start_scheduler=True, event_bus_dir=str(bus_dir), scheduler_interval_seconds=1)


def interval_pipeline(env: Env, with_event_trigger: bool = False) -> str:
    """A pipeline on a 5 s interval (the floor). With an event trigger too, it shows the bus's state."""
    p = env.call("POST", "/pipelines", json={"name": "keeps-running", "spec": {"tasks": [task("a")]}}).json()
    r = env.call("PUT", f"/pipelines/{p['id']}/schedule", json={"schedule": {"type": "interval", "seconds": 5}})
    assert r.status_code == 200, r.text
    if with_event_trigger:
        # Written as a save leaves it: the save itself refuses to turn a trigger on without a credential.
        stored = env.store.get_pipeline("acme", p["id"])
        from booth_pipeline.model import EventTrigger

        stored.event_trigger = EventTrigger.model_validate({"match": [{"storage": {"backendId": "local", "prefix": "inbox/"}}]})
        env.store.update_schedule(stored)
    return p["id"]


def scheduled_runs(env: Env, pid: str, n: int, timeout: float = 30) -> list[dict]:
    deadline = time.monotonic() + timeout
    runs: list[dict] = []
    while time.monotonic() < deadline:
        runs = [r for r in env.call("GET", f"/runs?pipelineId={pid}").json()["items"] if r["trigger"] == "schedule"]
        if len(runs) >= n:
            return runs
        time.sleep(0.25)
    return runs


def bus_state(env: Env, pid: str) -> dict:
    return env.call("GET", f"/pipelines/{pid}/event-trigger").json()["bus"]


def until(cond, timeout: float = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.2)
    return False


def assert_unaffected(env: Env, pid: str) -> None:
    """The pod is up and the schedule fires, twice, with the bus out of the picture."""
    assert env.client.get("/livez").status_code == 200 and env.client.get("/healthz").status_code == 200
    runs = scheduled_runs(env, pid, 2)
    assert len(runs) >= 2, runs
    first = env.wait_run(runs[-1]["id"])
    assert first["status"] == "succeeded", first
    assert env.app.state.event_bus._thread.is_alive()  # still retrying, on its own thread


def test_no_credential_at_all(tmp_path):
    bus_dir = credential_dir(tmp_path, None, creds=False)
    env = app(bus_dir)
    with env.client:
        pid = interval_pipeline(env, with_event_trigger=True)
        assert_unaffected(env, pid)
        state = bus_state(env, pid)
        assert state["state"] == "unavailable" and state["configured"] is False and "no event-bus credential" in state["reason"]
        assert env.call("GET", "/config").json()["eventTriggers"]["available"] is False
        # Core writes the Secret later: picked up without a restart.
        url = nats_url()
        fresh_stream(url)
        (bus_dir / "url").write_text(url)
        assert env.app.state.event_bus.ready.wait(20), env.app.state.event_bus.last_error
        assert until(lambda: bus_state(env, pid)["state"] == "connected")


@pytest.mark.parametrize("failure", ["rejected", "nkeys-missing"])
def test_a_credential_the_bus_rejects_or_the_client_cannot_use(tmp_path, monkeypatch, failure):
    """The original nkeys failure, and a bus that refuses the credential: both only end the consumer's session."""
    bus_dir = credential_dir(tmp_path, auth_url(), creds=True)
    if failure == "nkeys-missing":
        monkeypatch.setitem(sys.modules, "nkeys", None)  # `import nkeys` now fails, as it did in the image
    env = app(bus_dir)
    with env.client:
        pid = interval_pipeline(env, with_event_trigger=True)
        assert_unaffected(env, pid)
        state = bus_state(env, pid)
        assert state["state"] == "unavailable" and state["configured"] is True, state
        reason = state["reason"].lower()
        assert ("nkeys" in reason) if failure == "nkeys-missing" else ("authorization" in reason or "auth" in reason), state


def test_no_stream(tmp_path):
    from nats.js.errors import NotFoundError

    url = nats_url()

    from .test_event_bus_nats import admin

    async def drop(js):
        try:
            await js.delete_stream("BOOTH_EVENTS")
        except NotFoundError:
            pass

    admin(url, drop)
    env = app(credential_dir(tmp_path, url, creds=False))
    with env.client:
        pid = interval_pipeline(env, with_event_trigger=True)
        assert_unaffected(env, pid)
        assert until(lambda: "stream" in (bus_state(env, pid)["reason"] or "").lower() or "notfound" in (bus_state(env, pid)["reason"] or "").lower()), bus_state(env, pid)
        fresh_stream(url)  # core creates it: the consumer recovers on its own
        assert env.app.state.event_bus.ready.wait(20)
        assert until(lambda: bus_state(env, pid)["state"] == "connected")


class Proxy:
    """A TCP forwarder in front of the real NATS, so a test can take the bus away mid-session."""

    def __init__(self, target: str) -> None:
        host, port = target.removeprefix("nats://").split(":")
        self.target = (host, int(port))
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.conns: list[socket.socket] = []
        self.up = False

    @property
    def url(self) -> str:
        return f"nats://127.0.0.1:{self.port}"

    def start(self) -> None:
        if self.listener.fileno() == -1:
            self.listener = socket.socket()
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind(("127.0.0.1", self.port))
        self.listener.listen(8)
        self.up = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while self.up:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            upstream = socket.create_connection(self.target)
            self.conns += [client, upstream]
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()

    @staticmethod
    def _pipe(a: socket.socket, b: socket.socket) -> None:
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass

    def stop(self) -> None:
        self.up = False
        for s in [self.listener, *self.conns]:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
        self.conns = []


def test_the_bus_going_away_mid_session(tmp_path):
    url = nats_url()
    fresh_stream(url)
    proxy = Proxy(url)
    proxy.start()
    env = app(credential_dir(tmp_path, proxy.url, creds=False))
    try:
        with env.client:
            pid = interval_pipeline(env, with_event_trigger=True)
            bus = env.app.state.event_bus
            assert bus.ready.wait(20) and until(lambda: bus_state(env, pid)["state"] == "connected")
            proxy.stop()  # the bus is gone
            assert until(lambda: bus_state(env, pid)["state"] == "unavailable"), bus_state(env, pid)
            before = len(scheduled_runs(env, pid, 0, timeout=0))
            assert len(scheduled_runs(env, pid, before + 2)) >= before + 2  # the schedule kept firing meanwhile
            assert bus._thread.is_alive()
            proxy.start()  # and back: the consumer reconnects by itself, and events flow again
            assert until(lambda: bus_state(env, pid)["state"] == "connected", 30), bus_state(env, pid)
            publish(url, ["inbox/after-the-outage.csv"])
            assert until(lambda: (env.store.get_event_trigger_state("acme", pid) or object()).__dict__.get("event_count", 0) >= 1)
    finally:
        proxy.stop()
