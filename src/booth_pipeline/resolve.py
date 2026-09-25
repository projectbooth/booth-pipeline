"""Resolving a pipeline's DAG references into the task configs that actually run (ADR 0071),
and — since ADR 0073 — into whichever of a task's draft or a saved version a reference currently
means.

A ``PipelineSpec`` (draft or immutable version alike) may still hold a literal ``"latest"``
reference forever: ADR 0073 stops pinning it to a concrete version number at pipeline-save time, so
an "Always follow latest" reference is re-resolved fresh every time it's used here — at live
validation, at save-time compile-check, and at run start (``runs.py``) — which is what makes a
later plain Save on the referenced task visible to it immediately, matching what the UI already
promises. A reference pinned to a specific version number is completely unaffected: it always
resolves to that exact immutable snapshot, never a draft.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import ModelError, PipelineSpec, TaskConfig, TaskRef
from .records import TaskEntity
from .store.base import Store


@dataclass
class ResolvedTaskRef:
    entity: TaskEntity
    # None means this reference currently resolves to the task's draft, not any saved version.
    version: int | None
    config: TaskConfig


def resolve_task_ref(store: Store, workspace: str, ref: TaskRef, field: str) -> ResolvedTaskRef:
    """What ``ref`` means right now. Raises ``ModelError`` naming ``field`` if it cannot be
    resolved: the task no longer exists, a pinned version no longer exists, or a "latest"
    reference names a task with neither a draft nor any saved version yet."""
    entity = store.get_task(workspace, ref.task_id)
    if entity is None:
        raise ModelError(f"{field} references task {ref.task_id!r}, which does not exist", f"{field}.taskId")
    if ref.task_version == "latest":
        draft = store.get_task_draft(workspace, ref.task_id)
        if draft is not None:
            return ResolvedTaskRef(entity, None, draft.config)
        tv = store.get_task_version(workspace, ref.task_id, None)
        if tv is None:
            raise ModelError(f"{field} references task {ref.task_id!r}, which has no saved version or draft yet", f"{field}.taskId")
        return ResolvedTaskRef(entity, tv.version, tv.config)
    tv = store.get_task_version(workspace, ref.task_id, ref.task_version)
    if tv is None:
        raise ModelError(f"{field} references task {ref.task_id!r} version {ref.task_version}, which does not exist", f"{field}.taskVersion")
    return ResolvedTaskRef(entity, tv.version, tv.config)


def resolve_task_configs(store: Store, workspace: str, spec: PipelineSpec) -> dict[str, TaskConfig]:
    """``{ref.key: TaskConfig}`` for every node in ``spec``, resolved fresh — see module docstring
    for what "latest" means now. Raises ``ModelError`` naming the first reference that cannot be
    resolved."""
    return {ref.key: resolve_task_ref(store, workspace, ref, f"tasks[{i}]").config for i, ref in enumerate(spec.tasks)}
