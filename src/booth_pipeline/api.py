"""The HTTP API — reached through booth-core's gateway at ``/modules/pipeline/api/...``.

Every route needs ``Authorization: Bearer`` and ``X-Booth-Workspace`` (the gateway forwards the
validated ``X-Workspace`` under that name, ADR 0025). Errors are ``{"error": "...", "field"?: "..."}``
(422 validation, 409 conflict, 404, 403), the same envelope booth-catalog uses. Lists take
``?q=&limit=&offset=`` and return ``{"items": [...], "total": n}``.

Roles: any recognised role reads; ``editor`` and ``owner`` write and run (ADR 0038/0048 precedent).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from . import model as m
from .auth import Identity, require_read, require_write
from .records import RUN_TERMINAL, Job, LogLine, Pipeline, PipelineVersion, Run, TaskRun
from .schemas import JobInput, PipelineCreate, PipelineUpdate, ValidateRequest, VersionCreate
from .service import PipelineService

router = APIRouter(prefix="/api")

MAX_PAGE = 200


def svc(request: Request) -> PipelineService:
    return request.app.state.service


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _schedule(s: m.Schedule | None) -> dict[str, Any] | None:
    return s.model_dump(by_alias=True) if s else None


def pipeline_json(p: Pipeline) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "createdBy": p.created_by,
        "createdAt": _iso(p.created_at),
        "updatedAt": _iso(p.updated_at),
        "latestVersion": p.latest_version,
    }


def version_json(v: PipelineVersion, with_spec: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "pipelineId": v.pipeline_id,
        "version": v.version,
        "notes": v.notes,
        "createdBy": v.created_by,
        "createdAt": _iso(v.created_at),
        "taskCount": len(v.spec.tasks),
    }
    if with_spec:
        out["spec"] = v.spec.model_dump(by_alias=True, mode="json")
    return out


def job_json(j: Job) -> dict[str, Any]:
    return {
        "id": j.id,
        "name": j.name,
        "pipelineId": j.pipeline_id,
        "pipelineVersion": j.pipeline_version,
        "schedule": _schedule(j.schedule),
        "retry": j.retry.model_dump(by_alias=True) if j.retry else None,
        "allowConcurrentRuns": j.allow_concurrent_runs,
        "roleCeiling": j.role_ceiling,
        "hasOwner": bool(j.owner_sub),
        "createdBy": j.created_by,
        "createdAt": _iso(j.created_at),
        "updatedAt": _iso(j.updated_at),
        "nextRunAt": _iso(j.next_run_at),
    }


def run_json(r: Run) -> dict[str, Any]:
    return {
        "id": r.id,
        "jobId": r.job_id,
        "pipelineId": r.pipeline_id,
        "pipelineVersion": r.pipeline_version,
        "status": r.status,
        "trigger": r.trigger,
        "triggeredBy": r.triggered_by,
        "createdAt": _iso(r.created_at),
        "startedAt": _iso(r.started_at),
        "finishedAt": _iso(r.finished_at),
        "error": r.error,
        "cancelRequested": r.cancel_requested,
    }


def task_run_json(t: TaskRun) -> dict[str, Any]:
    return {
        "taskKey": t.task_key,
        "status": t.status,
        "attempts": t.attempts,
        "startedAt": _iso(t.started_at),
        "finishedAt": _iso(t.finished_at),
        "error": t.error,
    }


def log_json(ln: LogLine) -> dict[str, Any]:
    return {"seq": ln.seq, "ts": _iso(ln.ts), "taskKey": ln.task_key, "attempt": ln.attempt, "stream": ln.stream, "message": ln.message}


def page_json(items: list[Any], total: int, fn) -> dict[str, Any]:
    return {"items": [fn(i) for i in items], "total": total}


Limit = Query(50, ge=1, le=MAX_PAGE)
Offset = Query(0, ge=0)


# ---- discovery ----------------------------------------------------------------------------


@router.get("/config")
def config(request: Request, _: Identity = Depends(require_read)) -> dict[str, Any]:
    cfg = request.app.state.config
    return {
        "limits": {
            "maxTasks": m.MAX_TASKS,
            "maxInlineSourceBytes": m.MAX_INLINE_SOURCE_BYTES,
            "maxRetries": m.MAX_RETRIES,
            "maxTimeoutSeconds": m.MAX_TIMEOUT_SECONDS,
            "maxLogLinesPerRun": cfg.max_log_lines_per_run,
        },
        "catalogConfigured": svc(request).catalog.configured,
    }


@router.get("/runners")
def runners(request: Request, _: Identity = Depends(require_read)) -> dict[str, Any]:
    """Which runners a task may target. ``base`` is always there; Spark is listed but
    unavailable, with the reason (ADR 0006: never assumed, never a fallback)."""
    return {"items": [{"id": r.id, "displayName": r.display_name, "available": r.available, "reason": r.reason} for r in svc(request).registry.describe()]}


# ---- pipelines ----------------------------------------------------------------------------


@router.get("/pipelines")
def list_pipelines(request: Request, q: str = "", limit: int = Limit, offset: int = Offset, ident: Identity = Depends(require_read)):
    page = svc(request).list_pipelines(ident, q, limit, offset)
    return page_json(page.items, page.total, pipeline_json)


@router.post("/pipelines", status_code=201)
def create_pipeline(request: Request, body: PipelineCreate, ident: Identity = Depends(require_write)):
    p, v = svc(request).create_pipeline(ident, body.name, body.description, body.spec, body.notes)
    return {**pipeline_json(p), "version": version_json(v) if v else None}


@router.post("/pipelines/validate")
def validate_pipeline(request: Request, body: ValidateRequest, ident: Identity = Depends(require_read)):
    """Live check for the canvas: never saves, never calls the catalog. 200 with ``valid``
    rather than 422, because "this draft is not valid yet" is the normal state while drawing."""
    try:
        svc(request).validate_draft(body.spec)
    except m.ModelError as e:
        return {"valid": False, "error": e.message, "field": e.field}
    return {"valid": True}


@router.get("/pipelines/{pipeline_id}")
def get_pipeline(request: Request, pipeline_id: str, ident: Identity = Depends(require_read)):
    return pipeline_json(svc(request).get_pipeline(ident, pipeline_id))


@router.put("/pipelines/{pipeline_id}")
def update_pipeline(request: Request, pipeline_id: str, body: PipelineUpdate, ident: Identity = Depends(require_write)):
    return pipeline_json(svc(request).update_pipeline(ident, pipeline_id, body.name, body.description))


@router.delete("/pipelines/{pipeline_id}", status_code=204)
def delete_pipeline(request: Request, pipeline_id: str, ident: Identity = Depends(require_write)):
    svc(request).delete_pipeline(ident, pipeline_id)
    return Response(status_code=204)


@router.get("/pipelines/{pipeline_id}/versions")
def list_versions(request: Request, pipeline_id: str, limit: int = Limit, offset: int = Offset, ident: Identity = Depends(require_read)):
    page = svc(request).list_versions(ident, pipeline_id, limit, offset)
    return page_json(page.items, page.total, lambda v: version_json(v, with_spec=False))


@router.post("/pipelines/{pipeline_id}/versions", status_code=201)
def save_version(request: Request, pipeline_id: str, body: VersionCreate, ident: Identity = Depends(require_write)):
    return version_json(svc(request).save_version(ident, pipeline_id, body.spec, body.notes))


@router.get("/pipelines/{pipeline_id}/versions/{version}")
def get_version(request: Request, pipeline_id: str, version: str, ident: Identity = Depends(require_read)):
    """``version`` is a number, or ``latest``."""
    if version == "latest":
        n = None
    else:
        try:
            n = int(version)
        except ValueError:
            raise HTTPException(404, "pipeline version not found") from None
    return version_json(svc(request).get_version(ident, pipeline_id, n))


# ---- jobs ---------------------------------------------------------------------------------


@router.get("/jobs")
def list_jobs(request: Request, q: str = "", pipelineId: str | None = None, limit: int = Limit, offset: int = Offset, ident: Identity = Depends(require_read)):
    page = svc(request).list_jobs(ident, pipelineId, q, limit, offset)
    return page_json(page.items, page.total, job_json)


@router.post("/jobs", status_code=201)
def create_job(request: Request, body: JobInput, ident: Identity = Depends(require_write)):
    return job_json(svc(request).create_job(ident, body))


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str, ident: Identity = Depends(require_read)):
    return job_json(svc(request).get_job(ident, job_id))


@router.put("/jobs/{job_id}")
def update_job(request: Request, job_id: str, body: JobInput, ident: Identity = Depends(require_write)):
    return job_json(svc(request).update_job(ident, job_id, body))


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(request: Request, job_id: str, ident: Identity = Depends(require_write)):
    svc(request).delete_job(ident, job_id)
    return Response(status_code=204)


@router.post("/jobs/{job_id}/run", status_code=202)
def run_job(request: Request, job_id: str, ident: Identity = Depends(require_write)):
    """Run now. 202: the run is queued and executes asynchronously — poll ``GET /api/runs/{id}``."""
    return run_json(svc(request).run_now(ident, job_id))


# ---- runs ---------------------------------------------------------------------------------


@router.get("/runs")
def list_runs(request: Request, jobId: str | None = None, status: str | None = None, limit: int = Limit, offset: int = Offset, ident: Identity = Depends(require_read)):
    page = svc(request).list_runs(ident, jobId, status, limit, offset)
    return page_json(page.items, page.total, run_json)


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str, ident: Identity = Depends(require_read)):
    run, tasks = svc(request).get_run(ident, run_id)
    return {**run_json(run), "tasks": [task_run_json(t) for t in tasks]}


@router.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(request: Request, run_id: str, ident: Identity = Depends(require_write)):
    return run_json(svc(request).cancel_run(ident, run_id))


@router.get("/runs/{run_id}/logs")
def run_logs(
    request: Request,
    run_id: str,
    task: str | None = None,
    after: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1, le=5000),
    ident: Identity = Depends(require_read),
):
    """Log lines with ``seq > after`` — poll with the last ``seq`` you saw. ``task`` limits the
    lines to one task; without it you get the whole run, run-level (system) lines included.
    ``done`` says the run is finished *and* nothing more can arrive, so a poller can stop."""
    s = svc(request)
    # Order matters. The run's terminal status becomes visible only AFTER every log line is
    # flushed (runs.py closes the recorder before finish_run). So if the status we read is
    # terminal, the lines read *after* it are complete. Reading lines first would let a run finish
    # in between and report done=True with the tail of the log still missing.
    run, _ = s.get_run(ident, run_id)
    lines = s.logs(ident, run_id, task, after, limit)
    return {"items": [log_json(ln) for ln in lines], "done": run.status in RUN_TERMINAL and len(lines) < limit}
