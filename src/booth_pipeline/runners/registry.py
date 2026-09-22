"""Which runners exist on this install, and whether each is usable.

``base`` is always present. Anything else is opt-in: a task may name another runner only if it is
registered *and available* (ADR 0006 — Spark, or any other, is never assumed and never a fallback).

In v0 ``spark`` is listed as **unavailable**, with a reason, and cannot be selected. booth-spark
has not yet published the compute-submission interface it says it will define (its brief, ADR
0006), and inventing one here would be designing another module's contract for it. The seam
(``Runner``) and this registry are what a future adapter plugs into.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import Runner

SPARK_UNAVAILABLE = (
    "booth-spark does not yet define a compute-submission interface for pipelines to target "
    "(ADR 0006); a Spark runner can be wired in once it does"
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

    def describe(self) -> list[RunnerInfo]:
        out = [RunnerInfo(id=r, display_name=r.capitalize(), available=True) for r in sorted(self._runners)]
        if "spark" not in self._runners:
            out.append(RunnerInfo(id="spark", display_name="Spark", available=False, reason=SPARK_UNAVAILABLE))
        return out
