"""Runtime configuration, all from environment variables (the Helm chart sets them).

Names for the shared settings (``BOOTH_OIDC_*``) match booth-catalog/booth-storage so one set of
operator values drives every module. Module-specific ones are ``BOOTH_PIPELINE_*``.
"""

from __future__ import annotations

import os
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

    # Workload identity (ADR 0056/0058): the Secret core writes when the manifest declares
    # `workloadIdentity: {mint: true}`. Only ever readable by THIS (trusted) pod.
    workload_mint_dir: str = "/etc/booth/workload"
    # Where a task's ctx.storage / ctx.catalog send requests. Default: booth-core's gateway
    # (BOOTH_CORE_URL/modules/{storage,catalog}), which accepts a workload token exactly like a human
    # one (ADR 0059). An escape hatch for unusual network topology only: set these to reach a module's
    # own Service directly (the network policy must then allow it — runner.networkPolicy.egress.extra).
    storage_url: str = ""
    catalog_url: str = ""

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
            max_log_lines_per_run=_int("BOOTH_PIPELINE_MAX_LOG_LINES_PER_RUN", 50_000, 100),
            runner_python=os.environ.get("BOOTH_PIPELINE_RUNNER_PYTHON", ""),
            task_job_template=os.environ.get("BOOTH_PIPELINE_TASK_JOB_TEMPLATE", ""),
            max_concurrent_tasks=_int("BOOTH_PIPELINE_MAX_CONCURRENT_TASKS", 16, 1),
            task_start_timeout_seconds=_int("BOOTH_PIPELINE_TASK_START_TIMEOUT_SECONDS", 300, 10),
            task_name_prefix=os.environ.get("BOOTH_PIPELINE_TASK_NAME_PREFIX", "") or "booth-pipeline-task",
            workload_mint_dir=os.environ.get("BOOTH_WORKLOAD_MINT_DIR", "/etc/booth/workload"),
            storage_url=os.environ.get("BOOTH_PIPELINE_STORAGE_URL", "").rstrip("/"),
            catalog_url=os.environ.get("BOOTH_PIPELINE_CATALOG_URL", "").rstrip("/"),
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
        if self.stale_after_seconds <= self.heartbeat_seconds * 2:
            raise ConfigError("BOOTH_PIPELINE_STALE_AFTER_SECONDS must exceed twice the heartbeat interval, or healthy runs get failed")
