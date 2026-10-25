"""booth-core's project registry (ADR 0124; booth-architecture contracts/projects.md), read as the caller.

A project is a navigation label: pipelines and tasks may carry one, lists filter and group by it.
It has no authorization meaning, and nothing that decides who may do what, or when and as whom
anything runs, may read it (tests/unit/test_projects.py holds that line).

Pipeline uses the registry for two things only: checking a slug when one is assigned, and widening a
``?project=`` filter to the selected project's children. ``GET /api/projects`` is cached per workspace
for ``ttl`` seconds; the contract accepts brief staleness, since a project grants nothing. When the
registry can't be read (no core.url, a core without it, 503 until core's store is durable, or core
unreachable), assigning is refused with the reason and everything else carries on: lists filter by
the exact slug, and nothing is blocked.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

import httpx

SLUG_RE = re.compile(r"^[a-z0-9-]{1,63}$")  # the contract's slug grammar


class ProjectsUnavailable(Exception):
    """The registry couldn't be read; retryable, never the caller's fault."""


@dataclass(frozen=True)
class Project:
    slug: str
    name: str
    parent: str | None
    archived: bool


class ProjectsClient:
    def __init__(self, core_url: str, transport: httpx.BaseTransport | None = None, timeout: float = 5.0, ttl: float = 60.0) -> None:
        self._base = core_url.rstrip("/") if core_url else ""
        self._http = httpx.Client(transport=transport, timeout=timeout)
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, list[Project]]] = {}

    @property
    def configured(self) -> bool:
        return bool(self._base)

    def close(self) -> None:
        self._http.close()

    def list(self, token: str, workspace: str) -> list[Project]:
        """The workspace's projects, archived ones included, from the cache when it is fresh."""
        with self._lock:
            hit = self._cache.get(workspace)
            if hit and time.monotonic() - hit[0] < self._ttl:
                return hit[1]
        projects = self._fetch(token, workspace)
        with self._lock:
            self._cache[workspace] = (time.monotonic(), projects)
        return projects

    def _fetch(self, token: str, workspace: str) -> list[Project]:
        if not self._base:
            raise ProjectsUnavailable("projects are booth-core's, and this module has no core.url")
        try:
            r = self._http.get(f"{self._base}/api/projects", headers={"Authorization": f"Bearer {token}", "X-Workspace": workspace})
        except httpx.HTTPError as e:
            raise ProjectsUnavailable(f"booth-core is unreachable ({e.__class__.__name__})") from e
        if r.status_code == 404:
            raise ProjectsUnavailable("this booth-core has no project registry")
        if r.status_code != 200:
            raise ProjectsUnavailable(f"booth-core's project registry answered HTTP {r.status_code}")
        try:
            body = r.json()
            items = body["items"] if isinstance(body, dict) else body
            return [Project(str(p["slug"]), str(p.get("name") or p["slug"]), p.get("parent") or None, bool(p.get("archived"))) for p in items]
        except (ValueError, KeyError, TypeError) as e:
            raise ProjectsUnavailable("booth-core returned an unreadable project list") from e


def with_children(projects: Iterable[Project], slug: str) -> set[str]:
    """``slug`` and its children (two levels, per the contract): what ``?project=<slug>`` shows."""
    return {slug} | {p.slug for p in projects if p.parent == slug}
