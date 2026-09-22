"""An in-process Booth pipeline app with every external dependency faked: an in-memory store, a
token verifier that maps fixed strings to claims, and a mock booth-catalog behind httpx."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi.testclient import TestClient

from booth_pipeline.app import create_app
from booth_pipeline.auth import AuthError, Claims
from booth_pipeline.catalog_client import CatalogClient
from booth_pipeline.config import Config
from booth_pipeline.store.memory import MemoryStore

WS = "acme"

TOKENS = {
    "tok-owner": Claims("u-owner", "olga", ("/workspaces/acme/owner",)),
    "tok-editor": Claims("u-editor", "eddie", ("/workspaces/acme/editor",)),
    "tok-viewer": Claims("u-viewer", "vera", ("/workspaces/acme/viewer",)),
    "tok-other-ws": Claims("u-other", "otto", ("/workspaces/globex/owner",)),
    "tok-nowhere": Claims("u-none", "nobody", ()),
}


class FakeVerifier:
    def verify(self, raw_token: str) -> Claims:
        try:
            return TOKENS[raw_token]
        except KeyError:
            raise AuthError("unknown token") from None


@dataclass
class FakeCatalog:
    """A booth-catalog stand-in behaving as its docs/decisions/0006 guarantees."""

    # entry id -> {"name", "language", "versions": {label: source}, "order": [labels]}
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    down: bool = False  # simulate unreachable
    not_installed: bool = False  # the gateway's plain-text 404
    requests: list[httpx.Request] = field(default_factory=list)

    def add(self, entry_id: str, name: str, versions: dict[str, str], language: str = "python") -> None:
        self.entries[entry_id] = {"name": name, "language": language, "versions": dict(versions), "order": list(versions)}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("connection refused")
        if self.not_installed:
            return httpx.Response(404, text="module not found")
        parts = request.url.path.removeprefix("/modules/catalog/api/code/").split("/")
        entry = self.entries.get(parts[0])
        if entry is None:
            return httpx.Response(404, json={"error": "code entry not found"})
        if len(parts) == 1:
            return httpx.Response(200, json={"id": parts[0], "name": entry["name"], "language": entry["language"]})
        label = entry["order"][-1] if parts[2] == "latest" else parts[2]
        if label not in entry["versions"]:
            return httpx.Response(404, json={"error": "code entry or version not found"})
        return httpx.Response(200, json={"version": label, "seq": entry["order"].index(label) + 1, "source": entry["versions"][label]})


@dataclass
class Env:
    client: TestClient
    store: MemoryStore
    catalog: FakeCatalog
    app: Any

    def call(self, method: str, path: str, token: str = "tok-editor", json: Any = None, headers: dict[str, str] | None = None, ws: str = WS):
        h = {"Authorization": f"Bearer {token}", "X-Booth-Workspace": ws, **(headers or {})}
        return self.client.request(method, "/api" + path, json=json, headers=h)

    def wait_run(self, run_id: str, timeout: float = 60.0, token: str = "tok-editor") -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = self.call("GET", f"/runs/{run_id}", token).json()
            if r["status"] in ("succeeded", "failed", "canceled"):
                return r
            time.sleep(0.1)
        raise AssertionError(f"run {run_id} did not finish within {timeout}s: {r}")


def make_env(**cfg_overrides: Any) -> Env:
    cfg = Config(dev_memory=True, oidc_issuer_url="https://idp.test/realms/booth", oidc_client_id="booth-pipeline", **cfg_overrides)
    store = MemoryStore()
    catalog = FakeCatalog()
    client_catalog = CatalogClient("http://core.test", transport=httpx.MockTransport(catalog.handler))
    app = create_app(cfg, store=store, verifier=FakeVerifier(), catalog=client_catalog, start_scheduler=False)
    return Env(TestClient(app), store, catalog, app)


def task(key: str, kind: str = "transform", deps: list[str] | None = None, source: str = "def run(ctx):\n    return 1\n", **kw: Any) -> dict[str, Any]:
    return {"key": key, "kind": kind, "code": {"type": "inline", "source": source}, "dependsOn": deps or [], **kw}


def etl_spec() -> dict[str, Any]:
    return {
        "tasks": [
            task("extract", "source", source="def run(ctx):\n    print('extracting')\n    return [1, 2, 3]\n"),
            task("clean", "transform", ["extract"], source="def run(ctx):\n    return [x * 10 for x in ctx.inputs['extract']]\n"),
            task("load", "sink", ["clean"], source="def run(ctx):\n    print('loading', ctx.inputs['clean'])\n"),
        ]
    }
