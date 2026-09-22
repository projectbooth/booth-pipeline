"""Real HTTP servers for tests that must exercise the wire: the runner service (uvicorn), a fake
booth-core minting endpoint, and a fake booth-storage / booth-catalog behind the gateway prefix.
Real sockets matter here: cancellation is "the client hangs up", which no in-memory transport
reproduces faithfully."""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import uvicorn


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serve_asgi(app: Any):
    """Run an ASGI app on a real port in a thread; yields its base URL."""
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        t.join(timeout=10)


class Recorded:
    def __init__(self, method: str, path: str, headers: dict[str, str], body: bytes) -> None:
        self.method, self.path, self.headers, self.body = method, path, headers, body

    def json(self) -> Any:
        return json.loads(self.body)


class FakeHttp:
    """A tiny scriptable HTTP server that records every request it gets."""

    def __init__(self) -> None:
        self.requests: list[Recorded] = []
        self.handler = lambda req: (404, {"error": "not scripted"})  # (status, json-able | bytes)
        outer = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = Recorded(self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, self.rfile.read(n) if n else b"")
                outer.requests.append(req)
                status, body = outer.handler(req)
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream" if isinstance(body, bytes) else "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _do  # noqa: N815

            def log_message(self, *_):
                pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self._srv.server_port}"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    def __enter__(self) -> FakeHttp:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
