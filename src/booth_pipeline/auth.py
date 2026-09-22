"""Authentication and authorisation: this module's side of core-platform-api.md "Auth enforcement".

booth-core's gateway resolves the caller's workspace and role from the token's groups claim and
forwards them as ``X-Booth-Workspace`` / ``X-Booth-Role`` (ADR 0025). This module does NOT take
the forwarded role on trust (ADR 0041): verifying the token proves *who* the caller is, not that
they hold the role a header claims, and anything able to reach the pod without going through the
gateway could otherwise send a valid low-privilege token with a forged ``owner`` header. So:

* the token is verified here (signature via the IdP's JWKS, issuer, expiry, audience if required);
* the role is **re-derived from the token's own groups claim** for the requested workspace;
* a forwarded role stronger than the token grants is **rejected** with 403 — not downgraded.

Same claim grammar and same rejection rule as booth-catalog and booth-storage, so every module
fails closed the same way. This matters more here than most: a pipeline is code that runs.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Protocol

import httpx
import jwt
from fastapi import Depends, Header, HTTPException, Request
from jwt import PyJWKClient

HEADER_WORKSPACE = "X-Booth-Workspace"
HEADER_ROLE = "X-Booth-Role"

OWNER, EDITOR, VIEWER = "owner", "editor", "viewer"
_RANK = {OWNER: 3, EDITOR: 2, VIEWER: 1}

# ADR 0025's workspace-membership group shape.
_GROUP_RE = re.compile(r"^/workspaces/([a-z0-9-]+)/(owner|editor|viewer)$")

# Asymmetric algorithms only. Never HS* (a shared-secret "verification" against a public key is
# the classic algorithm-confusion attack) and never "none".
_ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Claims:
    subject: str
    display_name: str
    groups: tuple[str, ...]


class TokenVerifier(Protocol):
    def verify(self, raw_token: str) -> Claims: ...


class OIDCVerifier:
    """Verifies bearer tokens against the OIDC provider booth-core is configured against.

    Discovery is lazy and retried: the module must come up (and answer its health check) even if
    the identity provider is momentarily unreachable, and start verifying once it is — rather than
    crash-looping at boot on a dependency that has nothing to do with being healthy.
    """

    def __init__(self, issuer: str, client_id: str, require_audience: bool, groups_claim: str) -> None:
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._require_audience = require_audience
        self._groups_claim = groups_claim or "groups"
        self._jwks: PyJWKClient | None = None
        self._lock = threading.Lock()

    def _client(self) -> PyJWKClient:
        with self._lock:
            if self._jwks is None:
                resp = httpx.get(f"{self._issuer}/.well-known/openid-configuration", timeout=10)
                resp.raise_for_status()
                doc = resp.json()
                if doc.get("issuer", "").rstrip("/") != self._issuer:
                    raise AuthError("OIDC discovery document's issuer does not match the configured issuer")
                self._jwks = PyJWKClient(doc["jwks_uri"], cache_keys=True, lifespan=300)
            return self._jwks

    def verify(self, raw_token: str) -> Claims:
        try:
            key = self._client().get_signing_key_from_jwt(raw_token).key
            options = {"require": ["exp", "iss", "sub"], "verify_aud": self._require_audience}
            payload = jwt.decode(
                raw_token,
                key,
                algorithms=_ALGORITHMS,
                issuer=self._issuer,
                audience=self._client_id if self._require_audience else None,
                options=options,
            )
        except (jwt.PyJWTError, httpx.HTTPError, KeyError, ValueError) as e:
            raise AuthError(f"token verification failed: {e}") from e
        return claims_from_payload(payload, self._groups_claim)


def claims_from_payload(payload: dict, groups_claim: str) -> Claims:
    raw_groups = payload.get(groups_claim)
    # A claim of the wrong shape means "no groups" (fail closed), not an error: the token is
    # genuine, it simply grants no workspace role.
    groups = tuple(g for g in raw_groups if isinstance(g, str)) if isinstance(raw_groups, list) else ()
    display = payload["sub"]
    for name in ("email", "preferred_username"):  # later wins: preferred_username beats email
        v = payload.get(name)
        if isinstance(v, str) and v.strip():
            display = v.strip()
    return Claims(subject=payload["sub"], display_name=display, groups=groups)


def role_in_workspace(groups: tuple[str, ...] | list[str], workspace: str) -> str:
    """The highest role the groups grant in ``workspace``, or ""."""
    best = ""
    for g in groups:
        m = _GROUP_RE.match(g)
        if m and m.group(1) == workspace and _RANK[m.group(2)] > _RANK.get(best, 0):
            best = m.group(2)
    return best


def effective_role(forwarded: str, granted: str) -> str:
    """Never stronger than the token's grant, never stronger than the forwarded role (a gateway
    may legitimately narrow, never widen). Absent forwarded role => the token's."""
    if not forwarded:
        return granted
    if forwarded not in _RANK:
        return ""
    return forwarded if _RANK[forwarded] < _RANK.get(granted, 0) else granted


@dataclass(frozen=True)
class Identity:
    subject: str
    display_name: str
    workspace: str
    role: str
    token: str  # the caller's own bearer token, for calls made on their behalf (catalog lookup)

    @property
    def can_write(self) -> bool:
        """editor and owner write and run; viewer reads. Mirrors booth-storage's ADR 0038 and
        booth-catalog's ADR 0048 — no pipeline-specific permission model invented."""
        return self.role in (OWNER, EDITOR)


def bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def authenticate(verifier: TokenVerifier, authorization: str | None, workspace: str | None, forwarded_role: str | None) -> Identity:
    """The whole decision, as a pure function of the request's headers — so it is unit-testable
    without a server. Raises ``HTTPException`` with the status the contract calls for."""
    token = bearer_token(authorization)
    if not token:
        raise HTTPException(401, "missing bearer token")
    try:
        claims = verifier.verify(token)
    except AuthError:
        raise HTTPException(401, "invalid token") from None
    if not workspace:
        raise HTTPException(400, f"missing {HEADER_WORKSPACE} header")
    granted = role_in_workspace(claims.groups, workspace)
    if not granted:
        raise HTTPException(403, "your token grants no role in this workspace")
    forwarded = (forwarded_role or "").strip()
    # ADR 0041: a forwarded role stronger than the token grants is a forged or corrupted request.
    if _RANK.get(forwarded, 0) > _RANK[granted]:
        raise HTTPException(403, "the forwarded role exceeds what your token grants in this workspace")
    role = effective_role(forwarded, granted)
    if not role:
        raise HTTPException(403, "unrecognised role")
    return Identity(claims.subject, claims.display_name, workspace, role, token)


def identity_dependency(request: Request, authorization: str | None = Header(None), x_booth_workspace: str | None = Header(None), x_booth_role: str | None = Header(None)) -> Identity:
    return authenticate(request.app.state.verifier, authorization, x_booth_workspace, x_booth_role)


def require_read(identity: Identity = Depends(identity_dependency)) -> Identity:
    return identity


def require_write(identity: Identity = Depends(identity_dependency)) -> Identity:
    if not identity.can_write:
        raise HTTPException(403, "your role in this workspace is read-only")
    return identity
