"""Object storage from a task, through booth-core's credential sidecar (ADR 0095; docs/decisions/0019).

Executed *inside* a task's subprocess (both harnesses import it), never by the service. Standard
library only: it takes a DuckDB connection, it never imports DuckDB itself.

When a task has s3 credentials, its pod's s3-mode sidecar keeps two standard AWS files fresh and the
task gets the usual variables pointing at them: ``AWS_SHARED_CREDENTIALS_FILE`` (short-lived keys,
renewed before they expire) and ``AWS_CONFIG_FILE`` (the backend's ``endpoint_url``, ``region`` and
addressing style). boto3-based code reads both files unassisted. DuckDB reads the keys but **not**
the endpoint (contracts/credential-sidecar.md, "Known limitation"), so against a self-hosted backend
it would go to real AWS. ``duckdb_secret`` hands it the location; SQL tasks get that done for them.

Only the *location* is passed explicitly. The keys stay with DuckDB's own AWS credential chain,
which reads the sidecar's file (``REFRESH auto``), so a renewal reaches it with no code here, and no
key ever passes through this module or into SQL text. Same approach as booth-notebooks'
``booth.s3`` (its docs/decisions/0009).

Task pods have no internet by default, so the ``httpfs``/``aws`` extensions are baked into the image
(EXTENSION_DIR) and only LOADed; outside the image (local dev) they're installed on first use.
"""

from __future__ import annotations

import configparser
import os
import urllib.parse
from dataclasses import dataclass

# Where the image bakes DuckDB's httpfs and aws extensions (Dockerfile).
EXTENSION_DIR = "/opt/duckdb-extensions"

NOT_ENABLED = (
    "this task has no object-storage credentials (AWS_CONFIG_FILE / AWS_SHARED_CREDENTIALS_FILE aren't set): "
    "turn on Platform access for the task, and the deployment needs boothStorage.url and a lakehouse "
    "warehouse for this workspace"
)
NOT_READY = (
    "object-storage credentials aren't ready: the credential sidecar hasn't written its first lease "
    "(the task log says so if it timed out waiting); if it persists, the platform refused this task a lease"
)


class S3Unavailable(Exception):
    pass


@dataclass(frozen=True)
class Location:
    """Where this task's object storage is. ``endpoint_url`` is None for real AWS S3."""

    endpoint_url: str | None
    region: str | None
    addressing_style: str | None  # "path" | "virtual" | "auto" | None (unstated: the engine's default)

    @property
    def scheme(self) -> str | None:
        return urllib.parse.urlsplit(self.endpoint_url).scheme if self.endpoint_url else None

    @property
    def host(self) -> str | None:
        """``host[:port]``, the form DuckDB's ENDPOINT takes."""
        return urllib.parse.urlsplit(self.endpoint_url).netloc if self.endpoint_url else None


def _profile_section(name: str) -> str:
    # The AWS config file names a non-default profile "[profile <name>]"; the credentials file doesn't.
    return name if name == "default" else f"profile {name}"


def _nested(value: str) -> dict[str, str]:
    """botocore's nested form: ``s3 =`` then indented ``addressing_style = path``."""
    out = {}
    for line in value.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def location(env=None) -> Location:
    """Read the sidecar's config file (re-read on every call; it's tiny). Raises ``S3Unavailable``
    when the task has no s3 sidecar, or it hasn't written its first lease. Once the credentials file
    exists, a config file with no section for the profile means real AWS S3 (the sidecar writes none,
    having no endpoint to set): ``Location(None, None, None)``, the engine's own defaults.

    ``addressing_style`` is accepted both as a top-level key (what booth-core@330a178 writes) and
    nested under ``s3 =`` (botocore's documented form, which booth-core moves to per ADR 0095's sixth
    amendment), so the switch needs no change here."""
    env = os.environ if env is None else env
    conf_path, cred_path = env.get("AWS_CONFIG_FILE", ""), env.get("AWS_SHARED_CREDENTIALS_FILE", "")
    if not conf_path or not cred_path:
        raise S3Unavailable(NOT_ENABLED)
    if not os.path.exists(cred_path):
        raise S3Unavailable(NOT_READY)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(conf_path, encoding="utf-8")
    except configparser.Error as e:
        raise S3Unavailable(f"the credential sidecar's config file is unreadable: {e.__class__.__name__}") from None
    section = _profile_section(env.get("AWS_PROFILE", "") or "default")
    if not parser.has_section(section):
        return Location(None, None, None)
    sec = parser[section]
    style = _nested(sec.get("s3", "")).get("addressing_style") or sec.get("addressing_style")
    return Location(sec.get("endpoint_url") or None, sec.get("region") or None, style or None)


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# DuckDB's URL_STYLE names for the AWS addressing styles ("auto" and unstated: DuckDB's own default).
_URL_STYLE = {"path": "path", "virtual": "vhost"}


def secret_sql(loc: Location, name: str = "booth_s3") -> str:
    """The CREATE SECRET statement for ``loc``: the location only, keys from the credential chain."""
    if not name.replace("_", "").isalnum():
        raise ValueError("secret name must be letters, digits and underscores")
    parts = ["TYPE s3", "PROVIDER credential_chain", "REFRESH auto"]
    if loc.region:
        parts.append(f"REGION {_sql_str(loc.region)}")
    if loc.endpoint_url:
        parts.append(f"ENDPOINT {_sql_str(loc.host)}")
        parts.append(f"USE_SSL {'true' if loc.scheme == 'https' else 'false'}")
    if loc.addressing_style in _URL_STYLE:
        parts.append(f"URL_STYLE {_sql_str(_URL_STYLE[loc.addressing_style])}")
    return f"CREATE OR REPLACE SECRET {name} ({', '.join(parts)})"


def load_extensions(con, extension_dir: str = EXTENSION_DIR) -> None:
    """httpfs and aws: from the image's baked directory (no internet in a task pod), else installed."""
    if os.path.isdir(extension_dir):
        con.execute(f"SET extension_directory = {_sql_str(extension_dir)}")
        con.execute("LOAD httpfs; LOAD aws;")
    else:
        con.execute("INSTALL httpfs; LOAD httpfs; INSTALL aws; LOAD aws;")


def duckdb_secret(con, name: str = "booth_s3", env=None) -> Location:
    """Create (or replace) a DuckDB S3 secret on ``con`` for this task's backend, after loading the
    ``httpfs`` and ``aws`` extensions. Returns the location it used."""
    loc = location(env)
    load_extensions(con)
    con.execute(secret_sql(loc, name))
    return loc
