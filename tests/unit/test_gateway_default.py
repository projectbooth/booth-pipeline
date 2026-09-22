"""The default routing is booth-core's gateway (ADR 0059), and nothing assumes the direct-Service
workaround. The fake gateway here mimics what core's `/modules/{id}/*` does: strip the prefix, refuse
a request with no bearer or no X-Workspace, and set X-Booth-Workspace itself."""

from __future__ import annotations

import yaml

from booth_pipeline.config import Config
from booth_pipeline.runners.base import Cancellation, TaskAccess, TaskInvocation
from booth_pipeline.runners.subprocess_runner import SubprocessRunner

from .servers import FakeHttp

SRC = "def run(ctx):\n    ctx.storage.backends()\n    ctx.catalog.datasets()\n"


class Lines:
    def line(self, stream: str, message: str) -> None:
        pass


def run_task(access: TaskAccess) -> None:
    SubprocessRunner().run(TaskInvocation("r", "t", "sink", 1, SRC, {}, {}, 60, access), Lines(), Cancellation())


def gateway(fake: FakeHttp, seen: list[dict]) -> None:
    def handler(req):
        if not req.path.startswith("/modules/"):
            return 404, {"error": "not a gateway path"}
        module, _, rest = req.path.removeprefix("/modules/").partition("/")
        if not req.headers.get("authorization", "").startswith("Bearer ") or not req.headers.get("x-workspace"):
            return 401, {"error": "missing credentials"}
        seen.append({"module": module, "forwarded_path": "/" + rest, "headers": req.headers})
        return 200, {"items": [], "total": 0}

    fake.handler = handler


def test_with_no_overrides_tasks_call_the_gateway_and_the_default_urls_are_exactly_right():
    cfg = Config(core_url="http://booth-core.booth-system.svc:8080")
    assert cfg.storage_url == "" and cfg.catalog_url == ""  # nothing set: this is the DEFAULT, not an override
    assert cfg.storage_base == "http://booth-core.booth-system.svc:8080/modules/storage"
    assert cfg.catalog_base == "http://booth-core.booth-system.svc:8080/modules/catalog"


def test_through_the_gateway_only_x_workspace_is_sent_and_the_prefix_is_stripped_by_it():
    seen: list[dict] = []
    with FakeHttp() as core:
        gateway(core, seen)
        cfg = Config(core_url=core.url)
        run_task(TaskAccess("acme", "minted-1", cfg.storage_base, cfg.catalog_base))
    assert [(s["module"], s["forwarded_path"]) for s in seen] == [("storage", "/api/backends"), ("catalog", "/api/datasets?q=&limit=50")]
    for s in seen:
        assert s["headers"]["authorization"] == "Bearer minted-1" and s["headers"]["x-workspace"] == "acme"
        assert "x-booth-workspace" not in s["headers"]  # the gateway sets that itself; a client must not need to


def test_the_escape_hatch_still_works_and_then_also_sends_the_direct_call_header():
    with FakeHttp() as module:
        module.handler = lambda req: (200, {"items": []})
        cfg = Config(core_url="http://unused", storage_url=module.url, catalog_url=module.url)
        run_task(TaskAccess("acme", "tok", cfg.storage_base, cfg.catalog_base))
        assert [r.path for r in module.requests] == ["/api/backends", "/api/datasets?q=&limit=50"]  # exactly the configured base, no /modules
        assert all(r.headers["x-workspace"] == "acme" and r.headers["x-booth-workspace"] == "acme" for r in module.requests)


def test_the_chart_has_no_workaround_baked_into_its_defaults():
    values = yaml.safe_load(open("charts/booth-pipeline/values.yaml", encoding="utf-8"))
    egress = values["runner"]["networkPolicy"]["egress"]
    assert egress["extra"] == [] and egress["allowInternet"] is False
    assert "storageUrl" not in yaml.safe_dump(values) and "catalogUrl" not in yaml.safe_dump(values)
    env = _render_env()
    assert "BOOTH_PIPELINE_STORAGE_URL" not in env and "BOOTH_PIPELINE_CATALOG_URL" not in env
    assert env["BOOTH_CORE_URL"].endswith(":8080")  # the one URL tasks and the catalog client need


def _render_env() -> dict[str, str]:
    import shutil
    import subprocess

    if shutil.which("helm") is None:
        import os

        import pytest

        if os.environ.get("BOOTH_TEST_REQUIRE_EMULATORS") == "1":
            pytest.fail("helm is not installed but BOOTH_TEST_REQUIRE_EMULATORS=1")
        pytest.skip("helm not installed")
    out = subprocess.run(
        ["helm", "template", "t", "charts/booth-pipeline", "--set", "oidc.issuerUrl=https://k/r", "--set", "oidc.clientId=c", "--show-only", "templates/deployment.yaml"],
        capture_output=True, text=True, check=True,
    ).stdout
    dep = yaml.safe_load(out)
    return {e["name"]: e.get("value", "") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
