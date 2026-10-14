"""The build backend of the ``booth-pip-probe`` sdist (ADR 0115 Integration, tests/integration/pip.sh).

pip runs an sdist's build backend to make a wheel, and that is arbitrary code: here, it runs inside a
task pod's ``pip`` init container. So this backend IS the attacker the design has to contain. While
"building", it records what that container can reach and see:

* a TCP connect to every target the test lists (the index server serves the list, so this needs only
  the index itself), reported as open, timeout, refused or error:<Class>;
* its own mounts, its environment's variable names, whether anything listens on the credential
  sidecars' loopback ports, whether a ServiceAccount token is mounted;
* a scan of every readable file outside the OS and Python trees, and every visible process's
  environ and cmdline, for anything token-shaped, reported as hashes. A decoy planted first must be
  found, so a broken scan fails rather than passes.

The verdicts are baked into the wheel it builds (``booth_pip_probe.RESULT``), so the task's code
reads them back with a plain import: which also proves the package really was installed.

Standard library only: the build runs with build isolation and requires nothing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

NAME, VERSION = "booth_pip_probe", "1.0"
TOKENISH = re.compile(rb"[A-Za-z0-9_\-]{32,}")
SKIP = ("/proc", "/sys", "/usr", "/lib", "/lib64", "/bin", "/sbin", "/dev", "/boot")


def h(token: bytes | str) -> str:
    return hashlib.sha256(token.encode() if isinstance(token, str) else token).hexdigest()[:16]


def verdict(target: str, timeout: float = 5.0) -> str:
    host, port = target.rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=timeout).close()
        return "open"
    except TimeoutError:
        return "timeout"
    except ConnectionRefusedError:
        return "refused"
    except OSError as e:
        return f"error:{e.__class__.__name__}"


def scan() -> tuple[set[str], int]:
    found: set[str] = set()
    files = 0

    def read(path: str) -> None:
        nonlocal files
        try:
            with open(path, "rb") as f:
                data = f.read(1024 * 1024)
        except OSError:
            return
        files += 1
        found.update(h(m) for m in TOKENISH.findall(data))

    for root, dirs, names in os.walk("/"):
        if root == "/":
            dirs[:] = [d for d in dirs if "/" + d not in SKIP]
        for n in names:
            p = os.path.join(root, n)
            if os.path.isfile(p) and not os.path.islink(p):
                read(p)
    for pid in (p for p in os.listdir("/proc") if p.isdigit()):
        for part in ("environ", "cmdline"):
            read(f"/proc/{pid}/{part}")
    return found, files


def probe() -> dict:
    index = os.environ["PIP_INDEX_URL"].rstrip("/").removesuffix("/simple")
    with urllib.request.urlopen(f"{index}/probe-targets.json", timeout=10) as r:
        targets: dict[str, str] = json.load(r)
    with ThreadPoolExecutor(len(targets) or 1) as pool:
        verdicts = dict(zip(targets, pool.map(verdict, targets.values()), strict=True))
    decoy = "tok-decoy-" + secrets.token_hex(20)
    with open(os.path.join(os.environ.get("HOME", "/tmp"), "decoy.txt"), "w") as f:
        f.write(decoy)
    found, files = scan()
    with open("/proc/self/mounts") as f:
        mounts = sorted({line.split()[1] for line in f})
    return {
        "verdicts": verdicts,
        "mounts": mounts,
        "env_names": sorted(os.environ),
        "jwt_like_env_values": sum(1 for v in os.environ.values() if re.fullmatch(r"eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+", v)),
        "service_account_token": os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount"),
        "sidecar_ports": {p: verdict(f"127.0.0.1:{p}", 2) for p in ("5432", "8081")},
        "uid": os.getuid(),
        "decoy_found": h(decoy) in found,
        "token_hashes": sorted(found),
        "files_scanned": files,
    }


# ---- PEP 517 ----------------------------------------------------------------------------------------


def get_requires_for_build_wheel(config_settings=None):
    return []


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    result = probe()
    filename = f"{NAME}-{VERSION}-py3-none-any.whl"
    dist = f"{NAME}-{VERSION}.dist-info"
    files = {
        f"{NAME}/__init__.py": f"RESULT = {json.dumps(result)!r}\nimport json as _j\nRESULT = _j.loads(RESULT)\n".encode(),
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: booth-pip-probe\nVersion: {VERSION}\n".encode(),
        f"{dist}/WHEEL": b"Wheel-Version: 1.0\nGenerator: booth-pip-probe\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = []
    with zipfile.ZipFile(os.path.join(wheel_directory, filename), "w") as z:
        for path, data in files.items():
            z.writestr(path, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            record.append(f"{path},sha256={digest},{len(data)}")
        record.append(f"{dist}/RECORD,,")
        z.writestr(f"{dist}/RECORD", "\n".join(record) + "\n")
    return filename
