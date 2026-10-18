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
from .owners import is_service_account
from .records import (
    RUN_TERMINAL,
    LogLine,
    Pipeline,
    PipelineDraft,
    PipelineVersion,
    Run,
    TaskDraft,
    TaskEntity,
    TaskRun,
    TaskVersionRecord,
)
from .runners.kubejob import NO_PIP_ROUTE, PIP_ROUTES
from .schemas import (
    PipelineCreate,
    PipelineDraftSave,
    PipelineOwnerUpdate,
    PipelineScheduleUpdate,
    PipelineUpdate,
    TaskCreate,
    TaskDraftSave,
    TaskUpdate,
    TaskVersionCreate,
    ValidateRequest,
    VersionCreate,
)
from .service import PipelineService

router = APIRouter(prefix="/api")

MAX_PAGE = 200


def svc(request: Request) -> PipelineService:
    return request.app.state.service


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _schedule(s: m.Trigger | None) -> dict[str, Any] | None:
    return s.model_dump(by_alias=True) if s else None


def owner_json(owner: str, name: str, ident: Identity | None) -> dict[str, Any] | None:
    """Who runs act as (ADR 0120). A service account's id is shown; a person's ``sub`` is not
    (``you`` says whether it is the caller)."""
    if not owner:
        return None
    if is_service_account(owner):
        return {"kind": "service", "id": owner, "name": name or owner, "you": False}
    return {"kind": "person", "name": name, "you": ident is not None and ident.subject == owner}


def pipeline_json(p: Pipeline, ident: Identity | None = None) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "createdBy": p.created_by,
        "createdAt": _iso(p.created_at),
        "updatedAt": _iso(p.updated_at),
        "latestVersion": p.latest_version,
        # Scheduling/triggering/ownership, folded onto Pipeline directly (ADR 0071).
        "schedule": _schedule(p.schedule),
        "allowConcurrentRuns": p.allow_concurrent_runs,
        "roleCeiling": p.role_ceiling,
        "hasOwner": bool(p.owner_sub),
        "owner": owner_json(p.owner_sub, p.owner_name, ident),
        "nextRunAt": _iso(p.next_run_at),
        "pinnedVersion": p.pinned_version,
        # ADR 0073: whether a mutable draft has ever been saved (plain Save, or "Save as new
        # version", which also refreshes it) — separate from latestVersion above.
        "hasDraft": p.has_draft,
        "draftUpdatedAt": _iso(p.draft_updated_at),
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


def task_json(t: TaskEntity) -> dict[str, Any]:
    return {
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "createdBy": t.created_by,
        "createdAt": _iso(t.created_at),
        "updatedAt": _iso(t.updated_at),
        "latestVersion": t.latest_version,
        # ADR 0073: see pipeline_json's own hasDraft/draftUpdatedAt.
        "hasDraft": t.has_draft,
        "draftUpdatedAt": _iso(t.draft_updated_at),
    }


def task_version_json(v: TaskVersionRecord, with_config: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "taskId": v.task_id,
        "version": v.version,
        "notes": v.notes,
        "createdBy": v.created_by,
        "createdAt": _iso(v.created_at),
    }
    if with_config:
        out["config"] = v.config.model_dump(by_alias=True, mode="json")
    return out


def pipeline_draft_json(d: PipelineDraft) -> dict[str, Any]:
    return {"pipelineId": d.pipeline_id, "spec": d.spec.model_dump(by_alias=True, mode="json"), "updatedBy": d.updated_by, "updatedAt": _iso(d.updated_at)}


def task_draft_json(d: TaskDraft) -> dict[str, Any]:
    return {"taskId": d.task_id, "config": d.config.model_dump(by_alias=True, mode="json"), "updatedBy": d.updated_by, "updatedAt": _iso(d.updated_at)}


def run_json(r: Run, ident: Identity | None = None) -> dict[str, Any]:
    return {
        "id": r.id,
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
        # Who this run acted as: the pipeline's owner (scheduled) or who pressed Run now (manual).
        "actsAs": owner_json(r.owner_sub, r.owner_name, ident),
    }


def task_run_json(t: TaskRun, ident: Identity | None = None) -> dict[str, Any]:
    body = {
        "taskKey": t.task_key,
        "status": t.status,
        "attempts": t.attempts,
        "startedAt": _iso(t.started_at),
        "finishedAt": _iso(t.finished_at),
        "error": t.error,
        # ADR 0119: a Spark task's application ({kind, id, state, reason}); null otherwise.
        "external": t.external,
    }
    # What the task returned (runs.kept_output), for editors and owners only: like a Spark driver log, an
    # output can carry whatever the code put in it. Absent for viewers; null until an attempt succeeds.
    if ident is not None and ident.can_write:
        body["output"] = t.output
    return body


def log_json(ln: LogLine) -> dict[str, Any]:
    return {"seq": ln.seq, "ts": _iso(ln.ts), "taskKey": ln.task_key, "attempt": ln.attempt, "stream": ln.stream, "message": ln.message}


def page_json(items: list[Any], total: int, fn) -> dict[str, Any]:
    return {"items": [fn(i) for i in items], "total": total}


def _version_or_404(version: str, what: str) -> int | None:
    """``version`` is a number, or ``latest`` (None)."""
    if version == "latest":
        return None
    try:
        return int(version)
    except ValueError:
        raise HTTPException(404, f"{what} not found") from None


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
        "pip": pip_status(cfg),
        # ADR 0119: the Spark runner's settings the builder explains, when this install has it.
        "spark": {"retryMinDelaySeconds": cfg.spark_retry_min_delay_seconds} if "spark" in svc(request).registry.available_ids() else None,
    }


def pip_status(cfg: Any) -> dict[str, Any]:
    """ADR 0115: whether a task's Python requirements can be installed on this install, and if not,
    why. The builder warns with the reason; a task with requirements still saves."""
    if not cfg.task_job_template:
        return {"available": False, "reason": "packages are only installed for tasks run as Kubernetes Jobs; this install runs tasks in its own process (local development)"}
    if cfg.pip_route not in PIP_ROUTES:
        return {"available": False, "reason": NO_PIP_ROUTE}
    return {"available": True, "route": cfg.pip_route}


@router.get("/runners")
def runners(request: Request, ident: Identity = Depends(require_read)) -> dict[str, Any]:
    """Which runners a task may target. ``base`` is always there; Spark is listed but
    unavailable, with the reason (ADR 0006: never assumed, never a fallback)."""
    infos = svc(request).registry.describe(ident.token, ident.workspace)
    return {"items": [{"id": r.id, "displayName": r.display_name, "available": r.available, "reason": r.reason} for r in infos]}


# ---- pipelines -----------------------------------------------------------------------------


@router.get("/pipelines")
def list_pipelines(request: Request, q: str = "", limit: int = Limit, offset: int = Offset, ident: Identity = Depends(require_read)):
    page = svc(request).list_pipelines(ident, q, limit, offset)
    return page_json(page.items, page.total, lambda p: pipeline_json(p, ident))


@router.post("/pipelines", status_code=201)
def create_pipeline(request: Request, body: PipelineCreate, ident: Identity = Depends(require_write)):
    p, v = svc(request).create_pipeline(ident, body.name, body.description, body.spec, body.notes)
    return {**pipeline_json(p, ident), "version": version_json(v) if v else None}


@router.post("/pipelines/validate")
def validate_pipeline(request: Request, body: ValidateRequest, ident: Identity = Depends(require_read)):
    """Live check for the canvas: never saves, never calls the catalog or storage. 200 with
    ``valid`` rather than 422, because "this draft is not valid yet" is the normal state while
    drawing."""
    try:
        svc(request).validate_draft(ident, body.spec)
    except m.ModelError as e:
        return {"valid": False, "error": e.message, "field": e.field}
    return {"valid": True}


@router.get("/pipelines/{pipeline_id}")
def get_pipeline(request: Request, pipeline_id: str, ident: Identity = Depends(require_read)):
    return pipeline_json(svc(request).get_pipeline(ident, pipeline_id), ident)


@router.put("/pipelines/{pipeline_id}")
def update_pipeline(request: Request, pipeline_id: str, body: PipelineUpdate, ident: Identity = Depends(require_write)):
    return pipeline_json(svc(request).update_pipeline(ident, pipeline_id, body.name, body.description), ident)


@router.delete("/pipelines/{pipeline_id}", status_code=204)
def delete_pipeline(request: Request, pipeline_id: str, ident: Identity = Depends(require_write)):
    svc(request).delete_pipeline(ident, pipeline_id)
    return Response(status_code=204)


@router.put("/pipelines/{pipeline_id}/schedule")
def update_schedule(request: Request, pipeline_id: str, body: PipelineScheduleUpdate, ident: Identity = Depends(require_write)):
    return pipeline_json(svc(request).update_schedule(ident, pipeline_id, body.schedule, body.allow_concurrent_runs, body.role_ceiling, body.pinned_version), ident)


@router.put("/pipelines/{pipeline_id}/owner")
def set_owner(request: Request, pipeline_id: str, body: PipelineOwnerUpdate, ident: Identity = Depends(require_write)):
    """Take over (``"me"``), or, as a workspace owner, give the pipeline to a service account
    (``svc:<workspace>:<name>``). Never to another person (owners.py)."""
    return pipeline_json(svc(request).set_owner(ident, pipeline_id, body.owner), ident)


@router.get("/service-accounts/uses")
def service_account_uses(request: Request, ident: Identity = Depends(require_read)):
    """Which pipelines run as each service account. Core keeps no usage record (ADR 0120 addendum 2),
    so before disabling or deleting an account a workspace owner looks here."""
    return {
        "items": [
            {"account": account, "pipelines": [{"id": p.id, "name": p.name, "scheduled": bool(p.schedule and p.schedule.enabled)} for p in ps]}
            for account, ps in svc(request).service_account_uses(ident)
        ]
    }


@router.post("/pipelines/{pipeline_id}/run", status_code=202)
def run_pipeline(request: Request, pipeline_id: str, ident: Identity = Depends(require_write)):
    """Run now. 202: the run is queued and executes asynchronously — poll ``GET /api/runs/{id}``."""
    return run_json(svc(request).run_now(ident, pipeline_id), ident)


@router.get("/pipelines/{pipeline_id}/versions")
def list_versions(request: Request, pipeline_id: str, limit: int = Limit, offset: int = Offset, ident: Identity = Depends(require_read)):
    page = svc(request).list_versions(ident, pipeline_id, limit, offset)
    return page_json(page.items, page.total, lambda v: version_json(v, with_spec=False))


@router.post("/pipelines/{pipeline_id}/versions", status_code=201)
def save_version(request: Request, pipeline_id: str, body: VersionCreate, ident: Identity = Depends(require_write)):
    return version_json(svc(request).save_version(ident, pipeline_id, body.spec, body.notes))


@router.get("/pipelines/{pipeline_id}/versions/{version}")
def get_version(request: Request, pipeline_id: str, version: str, ident: Identity = Depends(require_read)):
    return version_json(svc(request).get_version(ident, pipeline_id, _version_or_404(version, "pipeline version")))


@router.get("/pipelines/{pipeline_id}/draft")
def get_pipeline_draft(request: Request, pipeline_id: str, ident: Identity = Depends(require_read)):
    d = svc(request).get_pipeline_draft(ident, pipeline_id)
    if d is None:
        raise HTTPException(404, "no draft saved for this pipeline")
    return pipeline_draft_json(d)


@router.put("/pipelines/{pipeline_id}/draft")
def save_pipeline_draft(request: Request, pipeline_id: str, body: PipelineDraftSave, ident: Identity = Depends(require_write)):
    """Plain "Save" (ADR 0073): writes the draft in place — no version created."""
    return pipeline_draft_json(svc(request).save_pipeline_draft(ident, pipeline_id, body.spec))


@router.get("/pipelines/{pipeline_id}/versions/{version}/export")
def export_version(request: Request, pipeline_id: str, version: str, ident: Identity = Depends(require_read)):
    """The version as a YAML document — export-only (ADR 0071 phase 4), but a complete, faithful
    structure (every referenced task resolved to its own config), shaped as the intended future
    import format too."""
    text = svc(request).export_version(ident, pipeline_id, _version_or_404(version, "pipeline version"))
    return Response(content=text, media_type="application/yaml")


# ---- tasks (ADR 0071: standalone, versioned, reusable — mirrors booth-catalog's own code versioning) ---


@router.get("/tasks")
def list_tasks(request: Request, q: str = "", limit: int = Limit, offset: int = Offset, ident: Identity = Depends(require_read)):
    page = svc(request).list_tasks(ident, q, limit, offset)
    return page_json(page.items, page.total, task_json)


@router.post("/tasks", status_code=201)
def create_task(request: Request, body: TaskCreate, ident: Identity = Depends(require_write)):
    t, v = svc(request).create_task(ident, body.name, body.description, body.config, body.notes)
    return {**task_json(t), "version": task_version_json(v) if v else None}


@router.get("/tasks/{task_id}")
def get_task(request: Request, task_id: str, ident: Identity = Depends(require_read)):
    return task_json(svc(request).get_task(ident, task_id))


@router.put("/tasks/{task_id}")
def update_task(request: Request, task_id: str, body: TaskUpdate, ident: Identity = Depends(require_write)):
    return task_json(svc(request).update_task(ident, task_id, body.name, body.description))


@router.delete("/tasks/{task_id}", status_code=204)
def delete_task(request: Request, task_id: str, ident: Identity = Depends(require_write)):
    svc(request).delete_task(ident, task_id)
    return Response(status_code=204)


@router.get("/tasks/{task_id}/versions")
def list_task_versions(request: Request, task_id: str, limit: int = Limit, offset: int = Offset, ident: Identity = Depends(require_read)):
    page = svc(request).list_task_versions(ident, task_id, limit, offset)
    return page_json(page.items, page.total, lambda v: task_version_json(v, with_config=False))


@router.post("/tasks/{task_id}/versions", status_code=201)
def save_task_version(request: Request, task_id: str, body: TaskVersionCreate, ident: Identity = Depends(require_write)):
    return task_version_json(svc(request).save_task_version(ident, task_id, body.config, body.notes))


@router.get("/tasks/{task_id}/versions/{version}")
def get_task_version(request: Request, task_id: str, version: str, ident: Identity = Depends(require_read)):
    return task_version_json(svc(request).get_task_version(ident, task_id, _version_or_404(version, "task version")))


@router.get("/tasks/{task_id}/draft")
def get_task_draft(request: Request, task_id: str, ident: Identity = Depends(require_read)):
    d = svc(request).get_task_draft(ident, task_id)
    if d is None:
        raise HTTPException(404, "no draft saved for this task")
    return task_draft_json(d)


@router.put("/tasks/{task_id}/draft")
def save_task_draft(request: Request, task_id: str, body: TaskDraftSave, ident: Identity = Depends(require_write)):
    """Plain "Save" (ADR 0073): writes the draft in place — no version created."""
    return task_draft_json(svc(request).save_task_draft(ident, task_id, body.config))


# ---- runs ---------------------------------------------------------------------------------


@router.get("/runs")
def list_runs(request: Request, pipelineId: str | None = None, status: str | None = None, limit: int = Limit, offset: int = Offset, ident: Identity = Depends(require_read)):
    page = svc(request).list_runs(ident, pipelineId, status, limit, offset)
    return page_json(page.items, page.total, lambda r: run_json(r, ident))


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str, ident: Identity = Depends(require_read)):
    run, tasks = svc(request).get_run(ident, run_id)
    return {**run_json(run, ident), "tasks": [task_run_json(t, ident) for t in tasks]}


@router.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(request: Request, run_id: str, ident: Identity = Depends(require_write)):
    return run_json(svc(request).cancel_run(ident, run_id), ident)


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
