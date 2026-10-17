"""Workload identity, minting side (ADR 0056/0058): getting a short-lived platform token for a run.

Only the trusted API/scheduler pod ever holds a working ``WorkloadMinter``: the minting credential
(Secret ``booth-workload-minting-credentials``) authorises "ask booth-core for a token" and is the
one thing that must never reach code a workspace member wrote (ADR 0057). The task pod receives only
the resulting token. ``config.py`` refuses to start if that Secret is present while tasks would run
in this same pod.

A token names the *run* (``sub`` = ``pipeline:<workspace>:<runId>``), never a person; core caps its
role at the lesser of our ``roleCeiling`` and what the run's ``owner`` holds in the workspace: a
person's live role, or a service account's stored role (``svc:<workspace>:<name>``, ADR 0120).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .owners import describe
from .runners.base import TaskAccess

log = logging.getLogger(__name__)

# What a person reads in the run log. For a person owner core answers every refusal with the same
# 403 (not signed in recently, no longer a member or demoted, or this module not entitled), so
# membership cannot be probed (ADR 0058; ADR 0119 amendment 3 point 5). The message names the
# likely causes and what to do, and does not claim to know which it was.
def owner_not_current(owner: str) -> str:
    return (
        f"booth-core would not issue a token for {owner}: they haven't signed in to Booth recently, no "
        "longer have the access this pipeline needs, or this module isn't entitled in this workspace. "
        "Ask them to sign in, or an editor can Take over the pipeline (or a workspace owner can make a "
        "service account its owner)."
    )


# For a service-account owner core says which (ADR 0120 addendum 2): members can see accounts anyway.
SERVICE_ACCOUNT_REFUSALS = {
    "service_account_disabled": "the pipeline's owner, service account {owner}, is disabled; a workspace owner can enable it or choose another owner",
    "service_account_not_found": "the pipeline's owner, service account {owner}, no longer exists; a workspace owner can choose another owner",
}


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

    def mint(self, workspace: str, subject: str, role_ceiling: str, owner: str, owner_name: str = "") -> WorkloadToken:
        body = {"workspace": workspace, "subject": subject, "roleCeiling": role_ceiling, "owner": owner}
        try:
            resp = self._http.post(self._url, json=body, headers={"Authorization": f"Bearer {self._credential}"})
        except httpx.HTTPError as e:
            raise MintUnavailable(f"booth-core is unreachable: {e.__class__.__name__}") from e
        if resp.status_code == 403:
            code = _error_code(resp)
            log.warning("mint refused (403%s) for %s in %s", f", {code}" if code else "", subject, workspace)
            if code in SERVICE_ACCOUNT_REFUSALS:
                raise MintRefused(SERVICE_ACCOUNT_REFUSALS[code].format(owner=owner))
            raise MintRefused(owner_not_current(describe(owner, owner_name)))
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


def _error_code(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    return str(body.get("code", "")) if isinstance(body, dict) else ""


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
        owner_name: str = "",
    ) -> None:
        self._minter = minter
        self._workspace, self._subject, self._ceiling, self._owner = workspace, subject, role_ceiling, owner
        self._owner_name = owner_name
        self._storage_url, self._catalog_url = storage_url, catalog_url

    def _mint(self) -> WorkloadToken:
        if self._minter is None:
            raise MintNotConfigured(
                "this deployment has no workload-identity credential, so tasks cannot be given access to storage or the catalog"
            )
        if not self._owner:
            raise MintRefused("this pipeline has no recorded owner (it predates workload identity); an editor can Take over the pipeline")
        return self._minter.mint(self._workspace, self._subject, self._ceiling, self._owner, self._owner_name)

    def authorize(self, attempts: int = 3, wait_seconds: float = 5.0, cancel: object = None) -> str:
        """ADR 0119 amendment 3: a scheduled run asks core once, before any task, whether it would issue
        a token for this run's owner, whatever the tasks need, so a pipeline whose owner has lapsed or
        whose service account is disabled runs nothing at all. The token is discarded: each task still
        mints its own. Returns the role core would grant. Raises MintRefused (with the reason) or, once
        core has been unreachable ``attempts`` times, MintUnavailable."""
        for i in range(attempts):
            try:
                return self._mint().role
            except MintUnavailable:
                if i == attempts - 1:
                    raise
                if cancel is not None and getattr(cancel, "canceled", False):
                    raise
                time.sleep(wait_seconds * (i + 1))
        raise AssertionError("unreachable")

    @property
    def configured(self) -> bool:
        return self._minter is not None

    def grant(self) -> TaskAccess:
        t = self._mint()
        return TaskAccess(
            workspace=self._workspace,
            token=t.token,
            storage_url=self._storage_url,
            catalog_url=self._catalog_url,
            refresh=lambda: self._mint().token,
            role=t.role,
        )
