"""Which runners exist on this install, and whether each is usable.

``base`` is always present. Anything else is opt-in: a task may name another runner only if it is
registered *and available* (ADR 0006 — Spark, or any other, is never assumed and never a fallback).

``spark`` (ADR 0119, runners/spark.py) is registered when this install can submit to booth-spark
as a run: core's gateway (``core.url``) and workload identity. Even then it is available only in a
workspace where booth-spark answers, which it checks live, as the caller. Not registered, it is
listed as unavailable with the reason, and can't be selected.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import Runner

SPARK_UNAVAILABLE = (
    "Spark tasks need booth-core's gateway (core.url) and workload identity on this install, "
    "and runner.spark.enabled (an operator setting)"
)


@dataclass(frozen=True)
class RunnerInfo:
    id: str
    display_name: str
    available: bool
    reason: str | None = None


class RunnerRegistry:
    def __init__(self, runners: list[Runner]) -> None:
        self._runners = {r.id: r for r in runners}

    def get(self, runner_id: str) -> Runner:
        try:
            return self._runners[runner_id]
        except KeyError:
            raise KeyError(f"runner {runner_id!r} is not available") from None

    def available_ids(self) -> set[str]:
        return set(self._runners)

    def availability(self, runner_id: str, token: str, workspace: str) -> str | None:
        """None if ``runner_id`` can run in this workspace for this caller, else why not."""
        runner = self._runners.get(runner_id)
        if runner is None:
            return f"runner {runner_id!r} is not available on this install"
        check = getattr(runner, "availability", None)
        return check(token, workspace) if callable(check) else None

    def describe(self, token: str | None = None, workspace: str | None = None) -> list[RunnerInfo]:
        out = []
        for r in sorted(self._runners):
            reason = self.availability(r, token, workspace) if token and workspace else None
            out.append(RunnerInfo(id=r, display_name=r.capitalize(), available=reason is None, reason=reason))
        if "spark" not in self._runners:
            out.append(RunnerInfo(id="spark", display_name="Spark", available=False, reason=SPARK_UNAVAILABLE))
        return out
