"""Executed *inside* the base runner's subprocess, never imported by the service.

Kept to the standard library on purpose: it runs under ``python -I`` (isolated mode: no user
site-packages, no PYTHON* environment variables), so it must not depend on anything installed
for the service itself.

Contract for task code (documented in the README):

* define ``run(ctx)`` — its JSON-serialisable return value is the task's output, handed to
  downstream tasks as ``ctx.inputs[<upstream task key>]``; or
* write a plain script with no ``run`` — it is executed top to bottom and its output is ``None``.

``print`` goes to the task's stdout log, ``ctx.log`` (a standard ``logging.Logger``) to stderr.
"""

import json
import logging
import os
import runpy
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

MAX_OUTPUT_BYTES = 1024 * 1024


class PlatformError(Exception):
    """A booth-storage / booth-catalog call failed. ``status`` is the HTTP status (0 = unreachable)."""

    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class _NoAccess:
    """ctx.storage / ctx.catalog for a task that did not opt in: fail loudly, with the fix."""

    def __init__(self, what):
        self._what = what

    def __getattr__(self, name):
        raise PlatformError(
            "ctx." + self._what + " is not available: this task has no platform access. "
            "Turn on 'Platform access' for the task in the pipeline builder (and save a new version)."
        )


class _Http:
    """A tiny authenticated client. The bearer token is re-read from a file on EVERY request: the
    API/scheduler pod keeps replacing it (a token lives ~10 minutes, a task can run for hours), and
    the minting credential itself never gets here (ADR 0057) - only this short-lived token does."""

    def __init__(self, base, workspace, token_file):
        self.base, self.workspace, self.token_file = base.rstrip("/"), workspace, token_file

    def _token(self):
        with open(self.token_file, encoding="utf-8") as f:
            return f.read().strip()

    def request(self, method, path, body=None, content_type=None, query=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        for attempt in (1, 2):
            req = urllib.request.Request(url, data=body, method=method)
            req.add_header("Authorization", "Bearer " + self._token())
            # Through booth-core's gateway (the default, ADR 0059) only X-Workspace is sent: the gateway
            # validates it against the token and sets X-Booth-Workspace itself. That second header is
            # what a module reads when called DIRECTLY, so it is added only for the base-URL escape
            # hatch (a base URL that is not a /modules/<id> gateway path).
            req.add_header("X-Workspace", self.workspace)
            if "/modules/" not in self.base:
                req.add_header("X-Booth-Workspace", self.workspace)
            if content_type:
                req.add_header("Content-Type", content_type)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.status, resp.read(), resp.headers
            except urllib.error.HTTPError as e:
                data = e.read()
                if e.code == 401 and attempt == 1:
                    time.sleep(1.5)  # a refresh may be landing right now: re-read the token once
                    continue
                message = data.decode("utf-8", "replace")[:300]
                try:
                    message = json.loads(data).get("error", message)
                except (ValueError, AttributeError):
                    pass
                if e.code == 401:
                    message += " (the task's platform token was rejected; it may have expired)"
                raise PlatformError("HTTP %d: %s" % (e.code, message), e.code) from None
            except (urllib.error.URLError, OSError) as e:
                raise PlatformError("could not reach the platform: %s" % e) from None

    def json(self, method, path, obj=None, query=None):
        body = None if obj is None else json.dumps(obj).encode("utf-8")
        _, data, _ = self.request(method, path, body, "application/json" if body is not None else None, query)
        return json.loads(data) if data else None


def _seg(value):
    return urllib.parse.quote(str(value), safe="")


class Storage:
    """booth-storage, as the run's identity. Locations are {backend, path} (ADR 0045)."""

    def __init__(self, http):
        self._h = http

    def backends(self):
        return self._h.json("GET", "/api/backends")

    def list(self, backend, prefix="", recursive=False):
        q = {"prefix": prefix}
        if recursive:
            q["recursive"] = "true"
        return self._h.json("GET", "/api/backends/%s/objects" % _seg(backend), query=q)

    def read(self, backend, path):
        _, data, _ = self._h.request("GET", "/api/backends/%s/objects/%s" % (_seg(backend), urllib.parse.quote(path, safe="/")))
        return data

    def read_text(self, backend, path, encoding="utf-8"):
        return self.read(backend, path).decode(encoding)

    def write(self, backend, path, data, content_type=None):
        if isinstance(data, str):
            data = data.encode("utf-8")
        _, out, _ = self._h.request(
            "PUT", "/api/backends/%s/objects/%s" % (_seg(backend), urllib.parse.quote(path, safe="/")), data, content_type or "application/octet-stream"
        )
        return json.loads(out) if out else None

    def delete(self, backend, path):
        self._h.request("DELETE", "/api/backends/%s/objects/%s" % (_seg(backend), urllib.parse.quote(path, safe="/")))


class Catalog:
    """booth-catalog, as the run's identity: find datasets and register what this run produced."""

    def __init__(self, http):
        self._h = http

    def datasets(self, q="", limit=50):
        return self._h.json("GET", "/api/datasets", query={"q": q, "limit": limit})

    def dataset(self, dataset_id):
        return self._h.json("GET", "/api/datasets/" + _seg(dataset_id))

    def register_dataset(self, name, backend, path, description="", schema=None, tags=None, owner=""):
        return self._h.json(
            "POST",
            "/api/datasets",
            {"name": name, "description": description, "location": {"backendId": backend, "path": path}, "schema": schema or [], "tags": tags or [], "owner": owner},
        )


class Context:
    def __init__(self, payload: dict) -> None:
        self.run_id = payload["runId"]
        self.task_key = payload["taskKey"]
        self.kind = payload["kind"]
        self.attempt = payload["attempt"]
        self.params = payload["params"]
        self.inputs = payload["inputs"]
        access = payload.get("access")
        token_file = os.environ.get("BOOTH_TOKEN_FILE")
        if access and token_file:
            self.storage = Storage(_Http(access["storageUrl"], access["workspace"], token_file))
            self.catalog = Catalog(_Http(access["catalogUrl"], access["workspace"], token_file))
        else:
            self.storage, self.catalog = _NoAccess("storage"), _NoAccess("catalog")
        logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
        self.log = logging.getLogger("task." + self.task_key)


def main() -> int:
    workdir, result_path = sys.argv[1], sys.argv[2]
    with open(os.path.join(workdir, "invocation.json"), encoding="utf-8") as f:
        payload = json.load(f)
    ctx = Context(payload)
    sys.path.insert(0, workdir)
    try:
        namespace = runpy.run_path(os.path.join(workdir, "task.py"), run_name="__booth_task__")
        run = namespace.get("run")
        output = run(ctx) if callable(run) else None
        try:
            encoded = json.dumps({"output": output})
        except (TypeError, ValueError) as e:
            print("task output is not JSON-serialisable: " + str(e), file=sys.stderr)
            return 3
        if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
            print(
                "task output exceeds " + str(MAX_OUTPUT_BYTES) + " bytes; pass large data through storage, not through ctx",
                file=sys.stderr,
            )
            return 3
        with open(result_path, "w", encoding="utf-8") as f:
            f.write(encoded)
        return 0
    except SystemExit as e:  # sys.exit(0) in a script is success; anything else is failure
        return 0 if e.code in (None, 0) else (e.code if isinstance(e.code, int) else 1)
    except BaseException:  # noqa: BLE001 - this is user code; report everything
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
