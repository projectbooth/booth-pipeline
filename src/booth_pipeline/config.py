"""Runtime configuration, all from environment variables (the Helm chart sets them).

Names for the shared settings (``BOOTH_OIDC_*``) match booth-catalog/booth-storage so one set of
operator values drives every module. Module-specific ones are ``BOOTH_PIPELINE_*``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field


class ConfigError(Exception):
    pass


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None or v == "" else v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int, lo: int = 0) -> int:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        n = int(v)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {v!r}") from None
    if n < lo:
        raise ConfigError(f"{name} must be >= {lo}, got {n}")
    return n


def _json_object(name: str) -> dict[str, str]:
    v = os.environ.get(name, "").strip()
    if not v:
        return {}
    try:
        obj = json.loads(v)
    except ValueError:
        raise ConfigError(f"{name} must be a JSON object, got {v[:80]!r}") from None
    if not isinstance(obj, dict):
        raise ConfigError(f"{name} must be a JSON object")
    return obj


# A runner.images name: what a task stores and the builder offers.
RUNNER_IMAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


@dataclass(frozen=True)
class Config:
    port: int = 8080

    # Persistence. Exactly one of these: a DSN (normal), or the in-memory store (dev only).
    database_dsn: str = ""
    dev_memory: bool = False

    # Identity — the same provider booth-core is configured against (ADR 0004). Every module
    # re-verifies the token itself (core-platform-api.md "Auth enforcement").
    oidc_issuer_url: str = ""
    # ADR 0108: fetch signing keys from here instead of the issuer's discovery document (the bundled
    # install's in-cluster IdP Service). `iss` is still checked against oidc_issuer_url. Empty = discovery.
    oidc_jwks_url: str = ""
    oidc_client_id: str = ""
    oidc_require_audience: bool = True
    oidc_groups_claim: str = "groups"  # must match booth-core's (ADR 0025)

    # booth-core's gateway, through which booth-catalog is reached (ADR 0007). Empty = the
    # catalog is not configured: catalog code references are refused, inline code still works.
    core_url: str = ""

    # Execution.
    max_workers: int = 4
    scheduler_interval_seconds: int = 15
    heartbeat_seconds: int = 10
    stale_after_seconds: int = 60
    queued_timeout_seconds: int = 900
    # Run retention (retention.py): finished runs older than this many days are pruned nightly, at
    # retention_hour_utc. 0: keep everything.
    retention_days: int = 0
    retention_hour_utc: int = 8
    max_log_lines_per_run: int = 50_000
    runner_python: str = ""
    runner_env_passthrough: tuple[str, ...] = field(default_factory=tuple)

    # Where tasks execute (ADR 0096). Set = one Kubernetes Job per task attempt, from this
    # chart-rendered Job template (JSON); the only deployed path. Empty = in a subprocess of THIS
    # process (local dev and tests only; refused alongside a workload-minting credential).
    task_job_template: str = ""
    max_concurrent_tasks: int = 16  # outstanding task Jobs (runner.maxConcurrent)
    task_start_timeout_seconds: int = 300  # Job created -> its pod Ready, before the task fails
    task_name_prefix: str = "booth-pipeline-task"
    # ADR 0115: where a task's Python packages can be installed from, as the chart works it out from
    # runner.pip and the task NetworkPolicy ("index", "internet", "extra", "unrestricted"); empty = no
    # route, so a task with requirements fails at once, naming the value to set.
    pip_route: str = ""
    # ADR 0119: the Spark runner (runner.spark): registered when core_url is set, workload identity is
    # configured and this is on. How often a Spark task's pod polls booth-spark, how long it waits for
    # capacity, and the least wait before retrying an attempt whose application ran (SeaweedFS).
    spark_enabled: bool = True
    spark_poll_seconds: int = 5
    spark_admission_wait_seconds: int = 900
    spark_retry_min_delay_seconds: int = 150

    # Workload identity (ADR 0056/0058): the Secret core writes when the manifest declares
    # `workloadIdentity: {mint: true}`. Only ever readable by THIS (trusted) pod.
    workload_mint_dir: str = "/etc/booth/workload"
    # Where a task's ctx.storage / ctx.catalog send requests. Default: booth-core's gateway
    # (BOOTH_CORE_URL/modules/{storage,catalog}), which accepts a workload token exactly like a human
    # one (ADR 0059). An escape hatch for unusual network topology only: set these to reach a module's
    # own Service directly (the network policy must then allow it — runner.networkPolicy.egress.extra).
    storage_url: str = ""
    catalog_url: str = ""

    # The event bus (ADR 0050, 0123): the Secret core writes when the manifest declares `events`
    # (`url` and `nats.creds`). BOOTH_PIPELINE_EVENTS_URL overrides the URL (tests run an
    # unauthenticated NATS with no credential file). No URL: no event trigger can be turned on.
    event_bus_dir: str = "/etc/booth/event-bus"
    event_bus_url_override: str = ""
    # Event triggers' JetStream consumer: how long a delivered event may wait for its ack before
    # it is redelivered, and how many may be outstanding at once.
    events_ack_wait_seconds: int = 30
    events_max_ack_pending: int = 1000

    # Task secrets (ADR 0128): the operator-created Kubernetes Secret holding the sealing key(s), one file
    # per key id (task_secrets.py). Mounted into this pod only. Absent: task secrets are off. With several
    # keys, secrets_key_id names the one that seals new values.
    secrets_key_dir: str = "/etc/booth/task-secrets-key"
    secrets_key_id: str = ""

    # runner.images: operator-built runner images a task may choose, name -> image pinned by digest. A
    # task Job's pod then runs that image (built FROM this module's image, plus packages) instead.
    runner_images: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Config:
        dev_memory = _bool("BOOTH_PIPELINE_DEV_MEMORY")
        dsn = os.environ.get("BOOTH_PIPELINE_DATABASE_DSN", "")
        cfg = cls(
            port=_int("BOOTH_PIPELINE_PORT", 8080, 1),
            database_dsn=dsn,
            dev_memory=dev_memory,
            oidc_issuer_url=os.environ.get("BOOTH_OIDC_ISSUER_URL", "").rstrip("/"),
            oidc_jwks_url=os.environ.get("BOOTH_OIDC_JWKS_URL", ""),
            oidc_client_id=os.environ.get("BOOTH_OIDC_CLIENT_ID", ""),
            oidc_require_audience=_bool("BOOTH_OIDC_REQUIRE_AUDIENCE", True),
            oidc_groups_claim=os.environ.get("BOOTH_OIDC_GROUPS_CLAIM", "") or "groups",
            core_url=os.environ.get("BOOTH_CORE_URL", "").rstrip("/"),
            max_workers=_int("BOOTH_PIPELINE_MAX_WORKERS", 4, 1),
            scheduler_interval_seconds=_int("BOOTH_PIPELINE_SCHEDULER_INTERVAL_SECONDS", 15, 1),
            heartbeat_seconds=_int("BOOTH_PIPELINE_HEARTBEAT_SECONDS", 10, 1),
            stale_after_seconds=_int("BOOTH_PIPELINE_STALE_AFTER_SECONDS", 60, 5),
            queued_timeout_seconds=_int("BOOTH_PIPELINE_QUEUED_TIMEOUT_SECONDS", 900, 30),
            retention_days=_int("BOOTH_PIPELINE_RETENTION_DAYS", 0, 0),
            retention_hour_utc=_int("BOOTH_PIPELINE_RETENTION_HOUR_UTC", 8, 0),
            max_log_lines_per_run=_int("BOOTH_PIPELINE_MAX_LOG_LINES_PER_RUN", 50_000, 100),
            runner_python=os.environ.get("BOOTH_PIPELINE_RUNNER_PYTHON", ""),
            task_job_template=os.environ.get("BOOTH_PIPELINE_TASK_JOB_TEMPLATE", ""),
            max_concurrent_tasks=_int("BOOTH_PIPELINE_MAX_CONCURRENT_TASKS", 16, 1),
            task_start_timeout_seconds=_int("BOOTH_PIPELINE_TASK_START_TIMEOUT_SECONDS", 300, 10),
            task_name_prefix=os.environ.get("BOOTH_PIPELINE_TASK_NAME_PREFIX", "") or "booth-pipeline-task",
            pip_route=os.environ.get("BOOTH_PIPELINE_PIP_ROUTE", ""),
            spark_enabled=_bool("BOOTH_PIPELINE_SPARK_ENABLED", True),
            spark_poll_seconds=_int("BOOTH_PIPELINE_SPARK_POLL_SECONDS", 5, 1),
            spark_admission_wait_seconds=_int("BOOTH_PIPELINE_SPARK_ADMISSION_WAIT_SECONDS", 900, 0),
            spark_retry_min_delay_seconds=_int("BOOTH_PIPELINE_SPARK_RETRY_MIN_DELAY_SECONDS", 150, 0),
            workload_mint_dir=os.environ.get("BOOTH_WORKLOAD_MINT_DIR", "/etc/booth/workload"),
            storage_url=os.environ.get("BOOTH_PIPELINE_STORAGE_URL", "").rstrip("/"),
            catalog_url=os.environ.get("BOOTH_PIPELINE_CATALOG_URL", "").rstrip("/"),
            event_bus_dir=os.environ.get("BOOTH_PIPELINE_EVENT_BUS_DIR", "/etc/booth/event-bus"),
            event_bus_url_override=os.environ.get("BOOTH_PIPELINE_EVENTS_URL", ""),
            events_ack_wait_seconds=_int("BOOTH_PIPELINE_EVENTS_ACK_WAIT_SECONDS", 30, 1),
            events_max_ack_pending=_int("BOOTH_PIPELINE_EVENTS_MAX_ACK_PENDING", 1000, 1),
            secrets_key_dir=os.environ.get("BOOTH_PIPELINE_SECRETS_KEY_DIR", "/etc/booth/task-secrets-key"),
            secrets_key_id=os.environ.get("BOOTH_PIPELINE_SECRETS_KEY_ID", ""),
            runner_images=_json_object("BOOTH_PIPELINE_RUNNER_IMAGES"),
            runner_env_passthrough=tuple(
                x.strip() for x in os.environ.get("BOOTH_PIPELINE_RUNNER_ENV_PASSTHROUGH", "").split(",") if x.strip()
            ),
        )
        cfg.validate()
        return cfg

    @property
    def storage_base(self) -> str:
        return self.storage_url or (f"{self.core_url}/modules/storage" if self.core_url else "")

    @property
    def catalog_base(self) -> str:
        return self.catalog_url or (f"{self.core_url}/modules/catalog" if self.core_url else "")

    @property
    def event_bus_url(self) -> str:
        if self.event_bus_url_override:
            return self.event_bus_url_override
        from pathlib import Path

        f = Path(self.event_bus_dir, "url")
        return f.read_text(encoding="utf-8").strip() if f.is_file() else ""

    @property
    def event_bus_creds_file(self) -> str:
        from pathlib import Path

        f = Path(self.event_bus_dir, "nats.creds")
        return str(f) if f.is_file() else ""

    def has_secrets_key(self) -> bool:
        from pathlib import Path

        d = Path(self.secrets_key_dir)
        return d.is_dir() and any(f.is_file() and not f.name.startswith(".") for f in d.iterdir())

    def has_minting_credential(self) -> bool:
        from pathlib import Path

        return Path(self.workload_mint_dir, "credential").is_file()

    def validate(self) -> None:
        if not self.task_job_template and self.has_minting_credential():
            # ADR 0057/0096: the credential that mints tokens must never share a pod with code a
            # workspace member wrote. With no Job template, tasks run as subprocesses HERE, and a task
            # could simply read the mounted Secret. Refuse to start rather than run in that state.
            raise ConfigError(
                f"a workload-minting credential is mounted at {self.workload_mint_dir} but BOOTH_PIPELINE_TASK_JOB_TEMPLATE is unset, "
                "so tasks would run in the same pod and could read it (ADR 0057/0096). Run tasks as Jobs, "
                "or do not give it the credential"
            )
        if not self.task_job_template and self.event_bus_creds_file:
            # The same rule for the bus credential: it reads every workspace's Storage and API events.
            raise ConfigError(
                f"an event-bus credential is mounted at {self.event_bus_dir} but BOOTH_PIPELINE_TASK_JOB_TEMPLATE is unset, "
                "so tasks would run in the same pod and could read it. Run tasks as Jobs, or do not give it the credential"
            )
        if self.has_secrets_key():
            if not self.task_job_template:
                # ADR 0128: the key opens every workspace's task secrets; the same rule as the credentials above.
                raise ConfigError(
                    f"a task secrets key is mounted at {self.secrets_key_dir} but BOOTH_PIPELINE_TASK_JOB_TEMPLATE is unset, "
                    "so tasks would run in the same pod and could read it (ADR 0128). Run tasks as Jobs, or do not mount the key"
                )
            from .task_secrets import SecretBox, SecretsError

            try:
                SecretBox.from_dir(self.secrets_key_dir, self.secrets_key_id)
            except SecretsError as e:
                raise ConfigError(f"task secrets key: {e}") from None
        for name, image in self.runner_images.items():
            if not isinstance(name, str) or not RUNNER_IMAGE_NAME_RE.match(name):
                raise ConfigError(f"runner.images: {name!r} is not a usable name (lower-case letters, digits and '-', up to 63)")
            if not isinstance(image, str) or not re.fullmatch(r"\S+@sha256:[0-9a-f]{64}", image):
                raise ConfigError(f"runner.images.{name} must be pinned by digest (registry/repo@sha256:<64 hex>), never a tag: {image!r}")
        if not self.dev_memory and not self.database_dsn:
            raise ConfigError(
                "BOOTH_PIPELINE_DATABASE_DSN is required (core provisions it as the "
                "booth-database-credentials Secret); set BOOTH_PIPELINE_DEV_MEMORY=true for a throwaway local run"
            )
        if self.oidc_jwks_url and not self.oidc_issuer_url:
            raise ConfigError("BOOTH_OIDC_JWKS_URL is set but BOOTH_OIDC_ISSUER_URL is empty: the issuer is still required to validate `iss`")
        if not self.oidc_issuer_url:
            raise ConfigError("BOOTH_OIDC_ISSUER_URL is required: every module verifies tokens itself")
        if self.oidc_require_audience and not self.oidc_client_id:
            raise ConfigError("BOOTH_OIDC_CLIENT_ID is required when BOOTH_OIDC_REQUIRE_AUDIENCE is true")
        if not 0 <= self.retention_hour_utc <= 23:
            raise ConfigError("BOOTH_PIPELINE_RETENTION_HOUR_UTC must be an hour, 0 to 23")
        if self.stale_after_seconds <= self.heartbeat_seconds * 2:
            raise ConfigError("BOOTH_PIPELINE_STALE_AFTER_SECONDS must exceed twice the heartbeat interval, or healthy runs get failed")
