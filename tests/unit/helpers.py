"""Shared builders and fakes for the unit tests."""

from __future__ import annotations

import threading
from typing import Any

from booth_pipeline.model import PipelineSpec


def task_ref(key: str, task_id: str = "t1", version: int | str = 1, deps: list[str] | None = None, **kw: Any) -> dict[str, Any]:
    """One DAG node (ADR 0071): a reference to a task's version, plus this pipeline's own wiring —
    what ``PipelineSpec.tasks`` holds now, never an embedded task definition."""
    return {"key": key, "taskId": task_id, "taskVersion": version, "dependsOn": deps or [], **kw}


def spec(*refs: dict[str, Any]) -> PipelineSpec:
    return PipelineSpec.model_validate({"tasks": list(refs)})


def linear() -> PipelineSpec:
    """extract -> clean -> load, the shape the brief's definition of done names."""
    return spec(
        task_ref("extract", task_id="t-extract"),
        task_ref("clean", task_id="t-clean", deps=["extract"]),
        task_ref("load", task_id="t-load", deps=["clean"]),
    )


def task_config(source: str = "def run(ctx):\n    return 1\n", **kw: Any) -> dict[str, Any]:
    """A standalone Task's own versioned config (ADR 0071) — what ``TaskConfig`` holds,
    independent of any pipeline node that references it."""
    return {"code": {"type": "inline", "source": source}, **kw}


class ListRecorder:
    """A ``RunRecorder`` that just remembers what it was told, in order."""

    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.lines: list[tuple[str, int, str, str]] = []
        self._lock = threading.Lock()

    def task_started(self, key: str, attempt: int) -> None:
        with self._lock:
            self.events.append(("started", key, attempt))

    def task_attempt_finished(self, key: str, attempt: int, status: str, error: str | None) -> None:
        with self._lock:
            self.events.append((status, key, attempt, error))

    def task_log(self, key: str, attempt: int) -> Any:
        rec = self

        class _L:
            def line(self, stream: str, message: str) -> None:
                with rec._lock:
                    rec.lines.append((key, attempt, stream, message))

        return _L()

    def system(self, message: str) -> None:
        with self._lock:
            self.events.append(("system", message))

    def statuses(self, key: str) -> list[str]:
        return [e[0] for e in self.events if len(e) > 1 and e[1] == key and e[0] != "system"]

    def log_text(self, key: str) -> list[str]:
        return [m for k, _a, _s, m in self.lines if k == key]
