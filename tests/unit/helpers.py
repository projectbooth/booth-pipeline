"""Shared builders and fakes for the unit tests."""

from __future__ import annotations

import threading
from typing import Any

from booth_pipeline.model import PipelineSpec, Task


def task(key: str, kind: str = "transform", deps: list[str] | None = None, source: str = "def run(ctx):\n    return 1\n", **kw: Any) -> dict[str, Any]:
    return {"key": key, "kind": kind, "code": {"type": "inline", "source": source}, "dependsOn": deps or [], **kw}


def spec(*tasks: dict[str, Any]) -> PipelineSpec:
    return PipelineSpec.model_validate({"tasks": list(tasks)})


def linear() -> PipelineSpec:
    """source -> transform -> sink, the shape the brief's definition of done names."""
    return spec(task("extract", "source"), task("clean", "transform", ["extract"]), task("load", "sink", ["clean"]))


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


def get(t: Task) -> str:
    return t.key
