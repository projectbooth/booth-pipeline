"""Builds the in-cluster test package index (ADR 0115 Integration), at image build time.

    python build_index.py SRC_DIR ROOT

ROOT/files already holds the real wheels, downloaded with --require-hashes from wheels.lock. This adds
the test's own packages, all sdists or wheels made here, offline:

* booth-pip-probe (sdist): its build backend probes the pip init container (probe_backend.py);
* booth-pip-broken (sdist): its build fails on purpose (broken_backend.py);
* booth-pip-trickle (wheel): served one byte every 5 seconds (server.py), so only the installer's own
  whole-install timeout can stop it;

then writes a PEP 503 "simple" index over all of them, and a small HTML page for the task to fetch
and parse.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import sys
import tarfile
import zipfile
from pathlib import Path

src, root = Path(sys.argv[1]), Path(sys.argv[2])
files, trickle, simple = root / "files", root / "trickle", root / "simple"
for d in (files, trickle, simple):
    d.mkdir(parents=True, exist_ok=True)


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sdist(name: str, backend: str) -> None:
    base = f"{name}-1.0"
    members = {
        "pyproject.toml": f'[build-system]\nrequires = []\nbuild-backend = "{backend}"\nbackend-path = ["."]\n',
        f"{backend}.py": (src / f"{backend}.py").read_text(),
        "PKG-INFO": f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n",
    }
    with tarfile.open(files / f"{base}.tar.gz", "w:gz") as tar:
        for path, text in members.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{base}/{path}")
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            tar.addfile(info, io.BytesIO(data))


def wheel(name: str, where: Path) -> None:
    mod, dist = name.replace("-", "_"), f"{name.replace('-', '_')}-1.0.dist-info"
    with zipfile.ZipFile(where / f"{mod}-1.0-py3-none-any.whl", "w") as z:
        z.writestr(f"{mod}/__init__.py", "")
        z.writestr(f"{dist}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
        z.writestr(f"{dist}/WHEEL", "Wheel-Version: 1.0\nGenerator: booth-ci\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        z.writestr(f"{dist}/RECORD", "")


sdist("booth-pip-probe", "probe_backend")
sdist("booth-pip-broken", "broken_backend")
wheel("booth-pip-trickle", trickle)

projects: dict[str, list[tuple[str, str]]] = {}
for d, prefix in ((files, "files"), (trickle, "trickle")):
    for f in sorted(d.iterdir()):
        project = normalize(re.split(r"-\d", f.name, maxsplit=1)[0])
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        projects.setdefault(project, []).append((f"/{prefix}/{f.name}#sha256={digest}", f.name))

for project, links in projects.items():
    (simple / project).mkdir(exist_ok=True)
    body = "".join(f'<a href="{href}">{name}</a><br>\n' for href, name in links)
    (simple / project / "index.html").write_text(f"<!DOCTYPE html><html><body>\n{body}</body></html>\n")
(simple / "index.html").write_text(
    "<!DOCTYPE html><html><body>\n" + "".join(f'<a href="/simple/{p}/">{p}</a><br>\n' for p in sorted(projects)) + "</body></html>\n"
)
(root / "page.html").write_text(
    '<html><body><h1 id="team">Booth Bears</h1><table><tr><td class="score">21</td><td class="score">17</td></tr></table></body></html>\n'
)
print("index:", {p: [n for _, n in ls] for p, ls in sorted(projects.items())}, file=sys.stderr)
os.chmod(root, 0o755)
