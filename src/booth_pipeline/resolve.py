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

ADR 0078 adds one step on top: each node's own ``paramOverrides`` are merged onto the config its
reference resolves to (``node_config``), so the same task referenced by several nodes can run with
a different value at each.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import ModelError, PipelineSpec, TaskConfig, TaskRef, check_params
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


def node_config(config: TaskConfig, ref: TaskRef, field: str) -> TaskConfig:
    """The config one DAG node actually runs with (ADR 0078): the referenced task's resolved
    ``config`` with this node's ``paramOverrides`` merged over its ``params`` — shallow, key by
    key, the node's value replacing the task's. A new object: the task's own config (which may be
    shared by other nodes referencing the same task) is never mutated. No overrides → ``config``
    itself, unchanged. Raises ``ModelError`` if the merge exceeds the params size cap."""
    if not ref.param_overrides:
        return config
    merged = {**config.params, **ref.param_overrides}
    try:
        check_params(merged, "params with this node's overrides")
    except ValueError as e:
        raise ModelError(str(e), f"{field}.paramOverrides") from None
    return config.model_copy(update={"params": merged})


def resolve_task_configs(store: Store, workspace: str, spec: PipelineSpec) -> dict[str, TaskConfig]:
    """``{ref.key: TaskConfig}`` for every node in ``spec``, resolved fresh — see module docstring
    for what "latest" means now — with each node's own param overrides applied (ADR 0078). Keyed
    by node, not by task, so two nodes referencing the same task each get their own params.
    Raises ``ModelError`` naming the first reference that cannot be resolved."""
    out: dict[str, TaskConfig] = {}
    for i, ref in enumerate(spec.tasks):
        field = f"tasks[{i}]"
        out[ref.key] = node_config(resolve_task_ref(store, workspace, ref, field).config, ref, field)
    return out
