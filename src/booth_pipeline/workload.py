"""Workload identity, minting side (ADR 0056/0058): getting a short-lived platform token for a run.

Only the trusted API/scheduler pod ever holds a working ``WorkloadMinter``: the minting credential
(Secret ``booth-workload-minting-credentials``) authorises "ask booth-core for a token" and is the
one thing that must never reach code a workspace member wrote (ADR 0057). The task pod receives only
the resulting token. ``config.py`` refuses to start if that Secret is present while tasks would run
in this same pod.

A token names the *run* (``sub`` = ``run:<id>``), never a person; core caps its role at the lesser of
our ``roleCeiling`` and what the run's ``owner`` (a user's ``sub``) currently holds in the workspace.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .runners.base import TaskAccess

log = logging.getLogger(__name__)

# What a person reads in the run log. Core deliberately answers 403 identically for "the owner's
# access is not current" and "this module is no longer entitled" (so membership cannot be probed),
# so the message names the overwhelmingly likely cause and does not overclaim.
OWNER_NOT_CURRENT = (
    "the owner of this run hasn't signed in to the platform recently enough to authorize its "
    "access to storage and the catalog (or no longer has access to this workspace). Ask them to "
    "sign in, or have someone who is active save the job to take it over."
)


class MintRefused(Exception):
    """Core said no (401/403). Expected, not a bug — and retrying will not change the answer."""


class MintUnavailable(Exception):
    """Core could not be reached or errored (5xx, network). Worth retrying."""


class MintNotConfigured(Exception):
    """This deployment has no minting credential, so no task can be given platform access."""


@dataclass(frozen=True)
class WorkloadToken:
    token: str
    expires_at: datetime
    role: str


class WorkloadMinter:
    def __init__(
        self,
        credential: str,
        url: str,
        issuer: str = "",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._credential = credential
        self._url = url
        self.issuer = issuer
        self._http = httpx.Client(transport=transport, timeout=timeout)

    @classmethod
    def from_dir(cls, directory: str) -> WorkloadMinter | None:
        """Load the Secret core wrote (keys ``credential``, ``url``, ``issuer``), or None if the
        module was not given one (it does not declare ``workloadIdentity``, or core is older)."""
        d = Path(directory)
        try:
            cred = (d / "credential").read_text(encoding="utf-8").strip()
            url = (d / "url").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not cred or not url:
            return None
        try:
            issuer = (d / "issuer").read_text(encoding="utf-8").strip()
        except OSError:
            issuer = ""
        return cls(cred, url, issuer)

    def close(self) -> None:
        self._http.close()

    def mint(self, workspace: str, subject: str, role_ceiling: str, owner: str) -> WorkloadToken:
        body = {"workspace": workspace, "subject": subject, "roleCeiling": role_ceiling, "owner": owner}
        try:
            resp = self._http.post(self._url, json=body, headers={"Authorization": f"Bearer {self._credential}"})
        except httpx.HTTPError as e:
            raise MintUnavailable(f"booth-core is unreachable: {e.__class__.__name__}") from e
        if resp.status_code == 403:
            log.warning("mint refused (403) for %s in %s", subject, workspace)
            raise MintRefused(OWNER_NOT_CURRENT)
        if resp.status_code == 401:
            # Our own credential was rejected: an operator problem, not the owner's.
            raise MintRefused(
                "booth-core rejected this module's workload-minting credential (401); an operator "
                "must check the booth-workload-minting-credentials Secret"
            )
        if resp.status_code >= 500:
            raise MintUnavailable(f"booth-core returned HTTP {resp.status_code} while minting")
        if resp.status_code != 200:
            raise MintUnavailable(f"booth-core rejected the mint request (HTTP {resp.status_code})")
        try:
            data = resp.json()
            expires = datetime.fromisoformat(str(data["expiresAt"]).replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            return WorkloadToken(str(data["token"]), expires, str(data.get("role", "")))
        except (ValueError, KeyError, TypeError) as e:
            raise MintUnavailable("booth-core returned an unreadable mint response") from e


class AccessProvider:
    """Hands out ``TaskAccess`` for one run. ``grant()`` mints fresh each call, so every task attempt
    starts with a full-lifetime token; ``refresh`` on the result is how a long-running task keeps one."""

    def __init__(
        self,
        minter: WorkloadMinter | None,
        *,
        workspace: str,
        subject: str,
        role_ceiling: str,
        owner: str,
        storage_url: str,
        catalog_url: str,
    ) -> None:
        self._minter = minter
        self._workspace, self._subject, self._ceiling, self._owner = workspace, subject, role_ceiling, owner
        self._storage_url, self._catalog_url = storage_url, catalog_url

    def _mint(self) -> WorkloadToken:
        if self._minter is None:
            raise MintNotConfigured(
                "this deployment has no workload-identity credential, so tasks cannot be given access to storage or the catalog"
            )
        if not self._owner:
            raise MintRefused("this job has no recorded owner (it predates workload identity); save it again to take ownership")
        return self._minter.mint(self._workspace, self._subject, self._ceiling, self._owner)

    def grant(self) -> TaskAccess:
        t = self._mint()
        return TaskAccess(
            workspace=self._workspace,
            token=t.token,
            storage_url=self._storage_url,
            catalog_url=self._catalog_url,
            refresh=lambda: self._mint().token,
        )
