"""booth-core's service accounts (ADR 0120; core's design note, "Route shapes"), read as the caller.

Pipeline needs one thing from them: before a workspace owner names an account as a pipeline's owner,
that the account exists in the workspace and isn't disabled. ``GET /api/service-accounts/{name}``
answers any member with ``{name, role, disabled}``, or 404. Core keeps no usage record (ADR 0120
addendum 2): which pipelines run as an account is this module's to show.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

import httpx


class ServiceAccountsUnavailable(Exception):
    """Core couldn't be asked (unset, unreachable, or an unexpected answer): retryable, not the caller's fault."""


@dataclass(frozen=True)
class ServiceAccount:
    name: str
    role: str
    disabled: bool


class ServiceAccountsClient:
    def __init__(self, core_url: str, transport: httpx.BaseTransport | None = None, timeout: float = 10.0) -> None:
        self._base = core_url.rstrip("/") if core_url else ""
        self._http = httpx.Client(transport=transport, timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self._base)

    def get(self, token: str, workspace: str, name: str) -> ServiceAccount | None:
        """The account, or None if the caller's workspace has none by that name."""
        if not self._base:
            raise ServiceAccountsUnavailable("service accounts are booth-core's, and this module has no core.url")
        try:
            r = self._http.get(
                f"{self._base}/api/service-accounts/{quote(name, safe='')}",
                headers={"Authorization": f"Bearer {token}", "X-Workspace": workspace},
            )
        except httpx.HTTPError as e:
            raise ServiceAccountsUnavailable(f"booth-core is unreachable ({e.__class__.__name__})") from e
        if r.status_code == 404:
            # Also what a core older than 0.3.7 answers (no such route): said as such by the caller.
            return None
        if r.status_code != 200:
            raise ServiceAccountsUnavailable(f"booth-core answered HTTP {r.status_code} for service account {name!r}")
        try:
            body = r.json()
            return ServiceAccount(str(body["name"]), str(body["role"]), bool(body["disabled"]))
        except (ValueError, KeyError, TypeError) as e:
            raise ServiceAccountsUnavailable("booth-core returned an unreadable service account") from e
