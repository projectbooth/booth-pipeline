"""A minimal in-cluster Kubernetes API client for ``KubernetesJobRunner`` (ADR 0096).

Only what one task's Job lifecycle needs — create/delete a Job, create a Secret, list pods by label —
over plain ``httpx`` (already this module's HTTP client) rather than the full ``kubernetes`` package
for four calls. Authenticated as the API/scheduler pod's own ServiceAccount, whose namespace-scoped
Role grants exactly: jobs create/get/list/watch/delete, pods get/list/watch, secrets create/delete
(charts/booth-pipeline/templates/rbac.yaml). Task pods never get a ServiceAccount token at all.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class KubeError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Kubernetes API {status}: {message}")
        self.status = status


class KubeClient:
    def __init__(self, base_url: str, namespace: str, token_file: str | None, transport: httpx.BaseTransport | None = None, ca_file: str | bool = True) -> None:
        self.namespace = namespace
        self._token_file = token_file
        self._http = httpx.Client(base_url=base_url.rstrip("/"), transport=transport, verify=ca_file, timeout=httpx.Timeout(15.0))

    @classmethod
    def in_cluster(cls) -> KubeClient:
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host:
            raise KubeError(0, "not running in a Kubernetes pod (KUBERNETES_SERVICE_HOST is unset)")
        if ":" in host:  # an IPv6 service address
            host = f"[{host}]"
        with open(f"{SA_DIR}/namespace", encoding="utf-8") as f:
            namespace = f.read().strip()
        return cls(f"https://{host}:{port}", namespace, f"{SA_DIR}/token", ca_file=f"{SA_DIR}/ca.crt")

    def close(self) -> None:
        self._http.close()

    def _headers(self) -> dict[str, str]:
        if not self._token_file:
            return {}
        # Re-read every call: the projected ServiceAccount token is rotated by the kubelet.
        with open(self._token_file, encoding="utf-8") as f:
            return {"Authorization": f"Bearer {f.read().strip()}"}

    def _call(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        r = self._http.request(method, path, headers=self._headers(), **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise KubeError(r.status_code, str(msg)[:500])
        return r.json() if r.content else {}

    # ---- the four operations a task's Job needs --------------------------------------------

    def create_job(self, job: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", f"/apis/batch/v1/namespaces/{self.namespace}/jobs", json=job)

    def delete_job(self, name: str) -> None:
        """Delete a Job and (via Background propagation) its pod; its owner-referenced Secret is
        garbage-collected with it. Already gone is fine."""
        try:
            self._call("DELETE", f"/apis/batch/v1/namespaces/{self.namespace}/jobs/{name}", json={"propagationPolicy": "Background"})
        except KubeError as e:
            if e.status != 404:
                raise

    def create_secret(self, secret: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", f"/api/v1/namespaces/{self.namespace}/secrets", json=secret)

    def list_pods(self, label_selector: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/api/v1/namespaces/{self.namespace}/pods", params={"labelSelector": label_selector}).get("items", [])
