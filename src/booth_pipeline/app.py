"""Application assembly: wires config -> store -> engine/runners -> service -> HTTP.

``create_app`` takes its collaborators as arguments (defaulting from config) so tests can inject
an in-memory store, a fake token verifier and a mock catalog transport — the same app, no real
IdP, no database, no network.
"""

from __future__ import annotations

import logging
import platform
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .api import router
from .auth import OIDCVerifier, TokenVerifier
from .catalog_client import CatalogClient
from .config import Config
from .model import ModelError
from .runners.base import Runner
from .runners.registry import RunnerRegistry
from .runners.remote import RemoteRunner
from .runners.subprocess_runner import SubprocessRunner
from .runs import RunManager
from .scheduler import Scheduler
from .service import JobBusy, NotFound, PipelineService, Unavailable
from .storage_client import StorageClient
from .store.base import Conflict, InUse, Store
from .workload import WorkloadMinter

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024 * 1024


def field_path(loc) -> str | None:
    """``("body", "spec", "tasks", 2, "dependsOn")`` -> ``spec.tasks[2].dependsOn`` — the same
    path syntax ``ModelError`` uses, so the UI has one format to map onto a form field or node."""
    out = ""
    for part in loc:
        if part == "body":
            continue
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or None


def build_store(cfg: Config) -> Store:
    if cfg.dev_memory:
        from .store.memory import MemoryStore

        log.warning("BOOTH_PIPELINE_DEV_MEMORY is set: state lives in memory and vanishes on exit")
        return MemoryStore()
    from .store.postgres import PostgresStore

    store = PostgresStore(cfg.database_dsn)
    store.migrate()
    return store


def create_app(
    cfg: Config,
    store: Store | None = None,
    verifier: TokenVerifier | None = None,
    catalog: CatalogClient | None = None,
    storage: StorageClient | None = None,
    start_scheduler: bool = True,
    minter: WorkloadMinter | None = None,
    runner_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    store = store or build_store(cfg)
    if cfg.runner_url:
        # Deployed default (ADR 0057): user code runs in the separate, credential-less runner pod.
        runner: Runner = RemoteRunner(cfg.runner_url, cfg.runner_secret(), transport=runner_transport)
    else:
        log.warning("BOOTH_PIPELINE_RUNNER_URL is unset: tasks run in subprocesses of THIS pod (fine for local dev; see docs/decisions/0002)")
        runner = SubprocessRunner(python=cfg.runner_python or None, env_passthrough=cfg.runner_env_passthrough)
    registry = RunnerRegistry([runner])
    catalog = catalog or CatalogClient(cfg.core_url)
    storage = storage or StorageClient(cfg.core_url)
    minter = minter if minter is not None else WorkloadMinter.from_dir(cfg.workload_mint_dir)
    worker_id = f"{platform.node()}-{uuid4().hex[:8]}"
    manager = RunManager(store, registry, cfg, worker_id, minter)
    service = PipelineService(store, registry, catalog, manager, storage)
    scheduler = Scheduler(store, service, manager, cfg.scheduler_interval_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_scheduler:
            scheduler.start()
        yield
        scheduler.stop()
        manager.shutdown()
        catalog.close()
        storage.close()
        for c in (minter, runner):
            close = getattr(c, "close", None)
            if close:
                close()
        close = getattr(store, "close", None)
        if close:
            close()

    app = FastAPI(title="booth-pipeline", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = cfg
    app.state.store = store
    app.state.service = service
    app.state.scheduler = scheduler
    app.state.verifier = verifier or OIDCVerifier(cfg.oidc_issuer_url, cfg.oidc_client_id, cfg.oidc_require_audience, cfg.oidc_groups_claim)

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"error": f"request body exceeds {MAX_BODY_BYTES} bytes"}, status_code=413)
        return await call_next(request)

    def err(status: int, message: str, field: str | None = None) -> JSONResponse:
        body = {"error": message}
        if field:
            body["field"] = field
        return JSONResponse(body, status_code=status)

    @app.exception_handler(ModelError)
    async def _model_error(_: Request, e: ModelError):
        return err(422, e.message, e.field)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError):
        first = e.errors()[0]
        return err(422, first["msg"].removeprefix("Value error, "), field_path(first["loc"]))

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, e: NotFound):
        return err(404, str(e))

    @app.exception_handler(Conflict)
    async def _conflict(_: Request, e: Conflict):
        return err(409, e.message, e.field)

    @app.exception_handler(InUse)
    async def _in_use(_: Request, e: InUse):
        return err(409, str(e))

    @app.exception_handler(JobBusy)
    async def _busy(_: Request, e: JobBusy):
        return err(409, str(e))

    @app.exception_handler(Unavailable)
    async def _unavailable(_: Request, e: Unavailable):
        return err(503, str(e))

    @app.exception_handler(HTTPException)
    async def _http(_: Request, e: HTTPException):
        return err(e.status_code, str(e.detail))

    @app.get("/healthz")
    def healthz():
        """Readiness — what booth-core polls (the manifest's healthCheckPath). Reports on this
        module's OWN database only. The catalog, storage, Spark and the identity provider are all
        optional or independently monitored; failing our health because one is down would take
        the whole module (and the base runner, which needs none of them) offline for nothing."""
        try:
            store.ping()
        except Exception:  # noqa: BLE001
            log.exception("health check: database unreachable")
            return JSONResponse({"status": "unavailable", "error": "database unreachable"}, status_code=503)
        return {"status": "ok", "module": "pipeline", "version": __version__}

    @app.get("/livez")
    def livez():
        return {"status": "ok"}

    app.include_router(router)
    return app
