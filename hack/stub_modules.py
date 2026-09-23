"""Throwaway stub servers for booth-catalog and booth-storage, for live UI bug repro only.
Not part of the shipped code. Run: python hack/stub_modules.py

Three servers:
 - catalog stub on :8073 (BOOTH_CATALOG_DEV_BACKEND for vite — the browser calls this directly)
 - storage stub on :8071 (BOOTH_STORAGE_DEV_BACKEND for vite — same, direct from the browser)
 - a combined "gateway" stub on :8072 (BOOTH_CORE_URL for the pipeline backend itself — its OWN
   save-time catalog_client.py/storage_client.py hit {core_url}/modules/{catalog,storage}/api/...,
   so this one process answers both prefixes on a single origin, the way a real gateway would)

:8080/:8082 are occupied by something else on this box, hence the odd ports.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Shapes below are copied from the REAL services' Go source, not guessed:
#   booth-catalog/internal/api/server.go (handleListCode, handleCodeVersions) +
#   booth-catalog/internal/code/model.go (Entry, VersionSummary)
#   booth-storage/internal/api/server.go (handleListBackends, handleListObjects) +
#   booth-storage/internal/backend/backend.go (ListResult, ObjectInfo)
CATALOG_ENTRIES = {
    "e1": {
        "id": "e1",
        "name": "loader",
        "description": "loads things",
        "language": "python",
        "latestVersion": {"version": "2.0.0", "seq": 2, "notes": ""},
        "versionCount": 2,
        "versions": {"1.0.0": "def run(ctx):\n    return 'v1'\n", "2.0.0": "def run(ctx):\n    return 'v2'\n"},
    },
    "e-fresh": {
        "id": "e-fresh",
        "name": "brand_new",
        "description": "just created a moment ago",
        "language": "python",
        "latestVersion": {"version": "1.0.0", "seq": 1, "notes": ""},
        "versionCount": 1,  # an entry ALWAYS has >=1 version in the real service — creating one requires it
        "versions": {"1.0.0": "def run(ctx):\n    return 'brand new'\n"},
    },
}

STORAGE_BACKENDS = [{"id": "b1", "displayName": "Main"}]
STORAGE_OBJECTS = {"b1": ["tasks/a.py", "tasks/b.sql"]}


def send(handler, status, body):
    data = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def catalog_route(handler, parts, q):
    """`parts` is the path split on '/', already past 'api'/'code' — e.g. ["code"], ["code","e1"]."""
    if parts == ["code"]:
        query = (q.get("q") or [""])[0].lower()
        items = [e for e in CATALOG_ENTRIES.values() if query in e["name"].lower()]
        return send(handler, 200, {"items": [{k: v for k, v in e.items() if k != "versions"} for e in items], "total": len(items)})
    if len(parts) == 2 and parts[0] == "code":
        e = CATALOG_ENTRIES.get(parts[1])
        if not e:
            return send(handler, 404, {"error": "code entry not found"})
        return send(handler, 200, {k: v for k, v in e.items() if k != "versions"})
    if len(parts) == 3 and parts[0] == "code" and parts[2] == "versions":
        e = CATALOG_ENTRIES.get(parts[1])
        if not e:
            return send(handler, 404, {"error": "code entry not found"})
        items = [{"version": v, "seq": i + 1, "notes": ""} for i, v in enumerate(e["versions"])]
        return send(handler, 200, {"versions": items})  # real shape: {"versions": [...]}, no "items"/"total"
    if len(parts) == 4 and parts[0] == "code" and parts[2] == "versions":
        e = CATALOG_ENTRIES.get(parts[1])
        label = parts[3]
        if not e or (label != "latest" and label not in e["versions"]):
            return send(handler, 404, {"error": "code entry or version not found"})
        if label == "latest":
            if not e["versions"]:
                return send(handler, 404, {"error": "no versions"})
            label = e["latestVersion"]["version"]
        return send(handler, 200, {"version": label, "seq": 1, "source": e["versions"][label]})
    return send(handler, 404, {"error": "not found"})


def storage_route(handler, parts, q):
    """`parts` is the path split on '/', already past 'api' — e.g. ["backends"], ["backends","b1","objects"]."""
    if parts == ["backends"]:
        return send(handler, 200, STORAGE_BACKENDS)  # real shape: a bare array, no "items" wrapper
    if len(parts) == 3 and parts[0] == "backends" and parts[2] == "objects":
        prefix = (q.get("prefix") or [""])[0]
        paths = [p for p in STORAGE_OBJECTS.get(parts[1], []) if p.startswith(prefix)]
        return send(handler, 200, {"entries": [{"path": p} for p in paths]})
    return send(handler, 404, {"error": "not found"})


class CatalogHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[catalog]", fmt % args)

    def do_GET(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        if parts[:1] != ["api"]:
            return send(self, 404, {"error": "not found"})
        catalog_route(self, parts[1:], parse_qs(u.query))


class StorageHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[storage]", fmt % args)

    def do_GET(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        if parts[:1] != ["api"]:
            return send(self, 404, {"error": "not found"})
        storage_route(self, parts[1:], parse_qs(u.query))


class GatewayHandler(BaseHTTPRequestHandler):
    """Mimics booth-core's gateway just enough for the pipeline backend's OWN save-time
    catalog/storage clients: {core_url}/modules/{catalog,storage}/api/... on one origin."""

    def log_message(self, fmt, *args):
        print("[gateway]", fmt % args)

    def do_GET(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        q = parse_qs(u.query)
        if parts[:3] == ["modules", "catalog", "api"]:
            return catalog_route(self, parts[3:], q)
        if parts[:3] == ["modules", "storage", "api"]:
            return storage_route(self, parts[3:], q)
        return send(self, 404, {"error": "not found"})


if __name__ == "__main__":
    import threading

    c = ThreadingHTTPServer(("localhost", 8073), CatalogHandler)
    s = ThreadingHTTPServer(("localhost", 8071), StorageHandler)
    g = ThreadingHTTPServer(("localhost", 8072), GatewayHandler)
    threading.Thread(target=c.serve_forever, daemon=True).start()
    threading.Thread(target=s.serve_forever, daemon=True).start()
    print("catalog stub on :8073, storage stub on :8071, gateway stub (BOOTH_CORE_URL) on :8072")
    g.serve_forever()
