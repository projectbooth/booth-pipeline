"""The one place this module talks to booth-storage for a task's CODE (ADR 0063) — resolving a
``{backendId, path}`` reference to a snapshot. Deliberately tiny, mirroring ``catalog_client.py``:
one GET, at **save time only**, made with the **caller's own token** through booth-core's gateway
(``/modules/storage/...``, ADR 0007). A run never calls storage for this: the source is
snapshotted into the spec before a run ever starts, same guarantee as a catalog reference.

Unrelated to ``ctx.storage`` (ADR 0056/0057): that is a *running task's own* opt-in access to
storage as the run's identity, wired through ``runners/_harness.py``. This client is how the
*service* fetches a task's code before it ever runs, using the person saving the pipeline's own
token — the two never share a code path.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

import httpx

STORAGE_PREFIX = "/modules/storage/api"

# Advisory only (mirrors CatalogCode.language: "an advisory label, checked against the runner at
# save"): inferred from the path's extension so a plain file browser needs no separate "language"
# field. Unrecognised extensions simply have no language ("" -> code_language() defaults to
# "python", exactly like an unset CatalogCode.language does).
_LANGUAGE_BY_EXTENSION = {".py": "python", ".sql": "sql"}


class StorageUnavailable(Exception):
    """booth-storage cannot be reached or is not installed."""


class StorageNotFound(Exception):
    """No object exists at that backend/path, or it is not visible in this workspace."""


class StorageDenied(Exception):
    """The caller's token was refused by storage."""


@dataclass(frozen=True)
class StorageCode:
    backend_id: str
    path: str
    language: str  # "" if the extension is not recognised
    source: str


class StorageClient:
    def __init__(self, core_url: str, transport: httpx.BaseTransport | None = None, timeout: float = 10.0) -> None:
        self._base = core_url.rstrip("/") + STORAGE_PREFIX if core_url else ""
        self._http = httpx.Client(transport=transport, timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self._base)

    def close(self) -> None:
        self._http.close()

    def fetch(self, token: str, workspace: str, backend_id: str, path: str) -> StorageCode:
        if not self._base:
            raise StorageUnavailable("booth-storage is not configured for this pipeline module (BOOTH_CORE_URL is unset)")
        url = f"{self._base}/backends/{quote(backend_id, safe='')}/objects/{quote(path, safe='/')}"
        try:
            resp = self._http.get(url, headers={"Authorization": f"Bearer {token}", "X-Workspace": workspace})
        except httpx.HTTPError as e:
            raise StorageUnavailable(f"booth-storage is unreachable: {e.__class__.__name__}") from e
        if resp.status_code == 404:
            # Same two-shapes-of-404 distinction as catalog_client.py: the module's own 404 is
            # JSON (the object is really missing); the gateway's is plain text (not installed).
            if _is_json_error(resp):
                raise StorageNotFound(resp.json().get("error", "not found"))
            raise StorageUnavailable("booth-storage is not installed (the gateway does not know a 'storage' module)")
        if resp.status_code in (401, 403):
            raise StorageDenied("booth-storage refused your credentials")
        if resp.status_code >= 500:
            raise StorageUnavailable(f"booth-storage returned HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise StorageUnavailable(f"booth-storage returned unexpected HTTP {resp.status_code}")
        try:
            source = resp.content.decode("utf-8")
        except UnicodeDecodeError:
            raise StorageUnavailable(f"the object at {path!r} is not UTF-8 text; only a text source file can back a task") from None
        return StorageCode(backend_id=backend_id, path=path, language=_language_for(path), source=source)


def _language_for(path: str) -> str:
    lower = path.lower()
    for ext, lang in _LANGUAGE_BY_EXTENSION.items():
        if lower.endswith(ext):
            return lang
    return ""


def _is_json_error(resp: httpx.Response) -> bool:
    if "json" not in resp.headers.get("content-type", ""):
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and "error" in body
