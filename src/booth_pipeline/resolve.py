"""Resolving a pipeline's DAG references into the task configs that actually run (ADR 0071).

A ``PipelineSpec`` pins each node to a specific ``(taskId, taskVersion)`` pair at save time — this
turns that pinned reference back into the ``TaskConfig`` it points at, for the engine to compile
and run against. Shared by the service (save-time compile-check and live validation) and the run
manager (execution), so both resolve a reference exactly the same way. Needs no catalog or storage
call: a task version's code was already snapshotted when that version was itself saved.
"""

from __future__ import annotations

from .model import ModelError, PipelineSpec, TaskConfig
from .store.base import Store


def resolve_task_configs(store: Store, workspace: str, spec: PipelineSpec) -> dict[str, TaskConfig]:
    """``{ref.key: TaskConfig}`` for every node in ``spec``. Raises ``ModelError`` naming the
    first reference that cannot be resolved: not yet pinned to a concrete version, or pinned to a
    task (or task version) that no longer exists — a reference can go stale if its task is deleted
    after the pipeline version pointing at it was saved."""
    out: dict[str, TaskConfig] = {}
    for i, ref in enumerate(spec.tasks):
        field = f"tasks[{i}]"
        if not ref.resolved:
            raise ModelError(f"{field} ({ref.key!r}) has not been pinned to a task version", f"{field}.taskVersion")
        tv = store.get_task_version(workspace, ref.task_id, ref.task_version)
        if tv is None:
            raise ModelError(
                f"{field} ({ref.key!r}) references task {ref.task_id!r} version {ref.task_version}, which no longer exists",
                f"{field}.taskId",
            )
        out[ref.key] = tv.config
    return out
