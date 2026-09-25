"""Exporting a pipeline version to YAML (ADR 0071 phase 4).

Export-only for now, but shaped as a faithful, complete structure — not a debug dump of internal
ids — since it is the intended future import format too: every referenced task is resolved to its
own name, description, pinned version and full config, alongside the DAG wiring and the pipeline's
own schedule. A saved pipeline version's references always resolve (``delete_task`` refuses to
remove a task still referenced by *any* pipeline version, past or present — see
``store/base.py``), so this never has to represent a dangling reference.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import yaml

from .model import ModelError
from .records import Pipeline, PipelineVersion
from .store.base import Store

API_VERSION = "booth-pipeline/v1"


def export_pipeline_version(store: Store, workspace: str, pipeline: Pipeline, version: PipelineVersion) -> str:
    """Render one immutable pipeline version as a YAML document (text)."""
    tasks: list[dict[str, Any]] = []
    for i, ref in enumerate(version.spec.tasks):
        entity = store.get_task(workspace, ref.task_id)
        tv = store.get_task_version(workspace, ref.task_id, ref.task_version)
        if entity is None or tv is None:
            # Should not happen (delete_task is refused while any version still references it) —
            # surfaced as a clear error rather than a silently incomplete export.
            raise ModelError(f"tasks[{i}] references task {ref.task_id!r} version {ref.task_version!r}, which no longer exists", f"tasks[{i}]")
        tasks.append(
            {
                "key": ref.key,
                "dependsOn": list(ref.depends_on),
                "position": {"x": ref.position.x, "y": ref.position.y},
                "task": {
                    "id": entity.id,
                    "name": entity.name,
                    "description": entity.description,
                    "version": tv.version,
                    "config": tv.config.model_dump(by_alias=True, mode="json"),
                },
            }
        )

    doc: dict[str, Any] = {
        "apiVersion": API_VERSION,
        "kind": "Pipeline",
        "metadata": {
            "id": pipeline.id,
            "name": pipeline.name,
            "description": pipeline.description,
            "version": version.version,
            "notes": version.notes,
            "exportedAt": datetime.now(UTC).isoformat(),
        },
        "spec": {
            "schedule": pipeline.schedule.model_dump(by_alias=True, mode="json") if pipeline.schedule else None,
            "allowConcurrentRuns": pipeline.allow_concurrent_runs,
            "roleCeiling": pipeline.role_ceiling,
            "pinnedVersion": pipeline.pinned_version,
            "tasks": tasks,
        },
    }
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True)
