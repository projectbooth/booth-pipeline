"""The one place this module talks to booth-catalog: resolving a code reference to a snapshot.

Deliberately tiny. Per ADR 0010 the code-catalog reference is a *narrow, pipeline-specific* shape,
``{entryId, version}``, and this client is the whole of our dependency on the catalog's API —
two GETs, at **save time only**, made with the **caller's own token** through booth-core's
gateway (``/modules/catalog/...``, ADR 0007). Runs never call the catalog.

What we rely on is exactly what booth-catalog recorded as guaranteed in its own
``docs/decisions/0006-code-source-stored-inline.md`` (confirmed 2026-09-21), and we test against
each: a published version's source is immutable; ``versions/latest`` returns the concrete
``version`` label; a deleted or foreign-workspace entry is 404. What it does *not* promise —
entry deletion cascades to its versions, the source cap is operator-configurable, ``language`` is a
free advisory label — we do not assume.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

import httpx

CATALOG_PREFIX = "/modules/catalog/api"


class CatalogUnavailable(Exception):
    """The catalog cannot be reached or is not installed. Inline code still works."""


class CatalogNotFound(Exception):
    """The entry (or that version of it) does not exist, or is not visible in this workspace."""


class CatalogDenied(Exception):
    """The caller's token was refused by the catalog."""


@dataclass(frozen=True)
class CatalogCode:
    entry_id: str
    entry_name: str
    language: str  # advisory free label, lowercase, "" if unset
    version: str  # the CONCRETE label — never "latest"
    source: str


class CatalogClient:
    def __init__(self, core_url: str, transport: httpx.BaseTransport | None = None, timeout: float = 10.0) -> None:
        self._base = core_url.rstrip("/") + CATALOG_PREFIX if core_url else ""
        self._http = httpx.Client(transport=transport, timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self._base)

    def close(self) -> None:
        self._http.close()

    def fetch(self, token: str, workspace: str, entry_id: str, version: str) -> CatalogCode:
        """Resolve ``{entry_id, version}`` (version may be ``"latest"``) to a concrete snapshot."""
        if not self._base:
            raise CatalogUnavailable("booth-catalog is not configured for this pipeline module (BOOTH_CORE_URL is unset)")
        eid = quote(entry_id, safe="")
        ver = self._get(token, workspace, f"/code/{eid}/versions/{quote(version, safe='')}")
        entry = self._get(token, workspace, f"/code/{eid}")
        concrete = ver.get("version")
        source = ver.get("source")
        if not isinstance(concrete, str) or not concrete or concrete == "latest" or not isinstance(source, str):
            raise CatalogUnavailable("booth-catalog returned an unexpected response for a code version")
        return CatalogCode(
            entry_id=entry_id,
            entry_name=str(entry.get("name", "")),
            language=str(entry.get("language", "") or ""),
            version=concrete,
            source=source,
        )

    def _get(self, token: str, workspace: str, path: str) -> dict:
        try:
            resp = self._http.get(self._base + path, headers={"Authorization": f"Bearer {token}", "X-Workspace": workspace})
        except httpx.HTTPError as e:
            raise CatalogUnavailable(f"booth-catalog is unreachable: {e.__class__.__name__}") from e
        if resp.status_code == 404:
            # Two different 404s: the catalog's own is JSON {"error": ...} (the entry is really
            # missing); the gateway's "module not found" is plain text (the catalog is not
            # installed). Only the body tells them apart.
            if _is_catalog_json_error(resp):
                raise CatalogNotFound(resp.json().get("error", "not found"))
            raise CatalogUnavailable("booth-catalog is not installed (the gateway does not know a 'catalog' module)")
        if resp.status_code in (401, 403):
            raise CatalogDenied("booth-catalog refused your credentials")
        if resp.status_code >= 500:
            raise CatalogUnavailable(f"booth-catalog returned HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise CatalogUnavailable(f"booth-catalog returned unexpected HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise CatalogUnavailable("booth-catalog returned a non-JSON response") from None
        if not isinstance(body, dict):
            raise CatalogUnavailable("booth-catalog returned an unexpected response shape")
        return body


def _is_catalog_json_error(resp: httpx.Response) -> bool:
    if "json" not in resp.headers.get("content-type", ""):
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and "error" in body
