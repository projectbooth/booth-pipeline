"""The in-cluster test package index (ADR 0115 Integration): static files, plus

* /trickle/...: one byte every 5 seconds, so pip's per-read --timeout never trips and only the
  installer's whole-install timeout can stop it;
* /probe-targets.json: the targets the booth-pip-probe build connects to, written by pip.sh into
  /tmp/probe-targets.json in this pod before each probe run.
"""

import http.server
import os
import time

ROOT = os.environ.get("INDEX_ROOT", "/srv/index")
TARGETS = "/tmp/probe-targets.json"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def do_GET(self):
        if self.path == "/probe-targets.json":
            try:
                body = open(TARGETS, "rb").read()
            except OSError:
                body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/trickle/"):
            path = os.path.join(ROOT, self.path.lstrip("/"))
            if not os.path.isfile(path):
                self.send_error(404)
                return
            data = open(path, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                for i in range(len(data)):
                    self.wfile.write(data[i : i + 1])
                    self.wfile.flush()
                    time.sleep(5)
            except OSError:
                pass  # the installer killed pip: exactly what is being tested
            return
        super().do_GET()


http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()  # noqa: S104
