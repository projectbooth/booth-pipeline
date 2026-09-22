"""PostgreSQL ``Store`` (psycopg 3, sync, pooled).

Sync on purpose: runs execute in worker threads and the API in FastAPI's threadpool, so a pooled
sync driver is the honest fit — no event loop to bridge.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import resources
from typing import Any
from uuid import uuid4

from psycopg import errors as pgerr
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import TypeAdapter

from ..model import PipelineSpec, RetryPolicy, Trigger
from ..records import (
    RUN_ACTIVE,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_TERMINAL,
    TASK_PENDING,
    Job,
    LogLine,
    Page,
    Pipeline,
    PipelineVersion,
    Run,
    TaskRun,
)
from ..schedule import next_fire_trigger
from .base import Busy, Conflict, InUse

# Arbitrary constant: the advisory lock that serialises schema migration across replicas that
# start at the same moment.
_MIGRATION_LOCK = 0x626F6F7470  # "boot p"

# A stored `schedule` JSON blob can be either union member (ADR 0065); dispatch on it rather than
# assuming CronTrigger, which would blow up on a stored IntervalTrigger row.
_trigger_adapter: TypeAdapter[Trigger] = TypeAdapter(Trigger)


def _like(q: str) -> str:
    """Escape LIKE metacharacters so a user's "50%_off" is matched literally."""
    return "%" + q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class PostgresStore:
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10) -> None:
        self._pool = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, kwargs={"row_factory": dict_row}, open=False, name="booth-pipeline"
        )
        self._pool.open(wait=True, timeout=30)

    def close(self) -> None:
        self._pool.close()

    # ---- migrations ----
    def migrate(self) -> None:
        files = sorted(
            (f for f in resources.files("booth_pipeline.store").joinpath("migrations").iterdir() if f.name.endswith(".sql")),
            key=lambda f: f.name,
        )
        with self._pool.connection() as conn:
            conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL)")
                conn.commit()
                done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
                for f in files:
                    if f.name in done:
                        continue
                    with conn.transaction():
                        conn.execute(f.read_text(encoding="utf-8"))
                        conn.execute("INSERT INTO schema_migrations (name, applied_at) VALUES (%s, %s)", (f.name, datetime.now(UTC)))
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))
                conn.commit()

    def ping(self) -> None:
        with self._pool.connection() as conn:
            conn.execute("SELECT 1")

    # ---- row mappers ----
    @staticmethod
    def _pipeline(r: dict[str, Any]) -> Pipeline:
        return Pipeline(r["id"], r["workspace"], r["name"], r["description"], r["created_by"], r["created_at"], r["updated_at"], r.get("latest_version") or 0)

    @staticmethod
    def _version(r: dict[str, Any]) -> PipelineVersion:
        return PipelineVersion(r["pipeline_id"], r["version"], PipelineSpec.model_validate(r["spec"]), r["notes"], r["created_by"], r["created_at"])

    @staticmethod
    def _job(r: dict[str, Any]) -> Job:
        return Job(
            id=r["id"],
            workspace=r["workspace"],
            name=r["name"],
            pipeline_id=r["pipeline_id"],
            pipeline_version=r["pipeline_version"],
            schedule=_trigger_adapter.validate_python(r["schedule"]) if r["schedule"] else None,
            retry=RetryPolicy.model_validate(r["retry"]) if r["retry"] else None,
            allow_concurrent_runs=r["allow_concurrent_runs"],
            created_by=r["created_by"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            next_run_at=r["next_run_at"],
            owner_sub=r["owner_sub"],
            role_ceiling=r["role_ceiling"],
        )

    @staticmethod
    def _run(r: dict[str, Any]) -> Run:
        return Run(
            id=r["id"], workspace=r["workspace"], job_id=r["job_id"], pipeline_id=r["pipeline_id"],
            pipeline_version=r["pipeline_version"], status=r["status"], trigger=r["trigger"],
            triggered_by=r["triggered_by"], created_at=r["created_at"], started_at=r["started_at"],
            finished_at=r["finished_at"], error=r["error"], worker_id=r["worker_id"],
            heartbeat_at=r["heartbeat_at"], cancel_requested=r["cancel_requested"],
            owner_sub=r["owner_sub"],
        )  # fmt: skip

    # ---- pipelines ----
    _PIPELINE_SELECT = """
        SELECT p.*, COALESCE((SELECT max(version) FROM pipeline_versions v WHERE v.pipeline_id = p.id), 0) AS latest_version
        FROM pipelines p
    """

    def create_pipeline(self, workspace, name, description, by):
        now, pid = datetime.now(UTC), str(uuid4())
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    "INSERT INTO pipelines (id, workspace, name, description, created_by, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (pid, workspace, name, description, by, now, now),
                )
        except pgerr.UniqueViolation:
            raise Conflict("a pipeline with that name already exists in this workspace", "name") from None
        return Pipeline(pid, workspace, name, description, by, now, now)

    def get_pipeline(self, workspace, pipeline_id):
        with self._pool.connection() as conn:
            r = conn.execute(self._PIPELINE_SELECT + " WHERE p.workspace = %s AND p.id = %s", (workspace, pipeline_id)).fetchone()
        return self._pipeline(r) if r else None

    def list_pipelines(self, workspace, q, limit, offset):
        where, args = "p.workspace = %s", [workspace]
        if q.strip():
            where += " AND (lower(p.name) LIKE %s ESCAPE '\\' OR lower(p.description) LIKE %s ESCAPE '\\')"
            args += [_like(q), _like(q)]
        with self._pool.connection() as conn:
            total = conn.execute(f"SELECT count(*) AS n FROM pipelines p WHERE {where}", args).fetchone()["n"]
            rows = conn.execute(
                f"{self._PIPELINE_SELECT} WHERE {where} ORDER BY lower(p.name), p.id LIMIT %s OFFSET %s", [*args, limit, offset]
            ).fetchall()
        return Page([self._pipeline(r) for r in rows], total)

    def update_pipeline(self, workspace, pipeline_id, name, description):
        try:
            with self._pool.connection() as conn:
                cur = conn.execute(
                    "UPDATE pipelines SET name=%s, description=%s, updated_at=%s WHERE workspace=%s AND id=%s",
                    (name, description, datetime.now(UTC), workspace, pipeline_id),
                )
                if cur.rowcount == 0:
                    return None
        except pgerr.UniqueViolation:
            raise Conflict("a pipeline with that name already exists in this workspace", "name") from None
        return self.get_pipeline(workspace, pipeline_id)

    def delete_pipeline(self, workspace, pipeline_id):
        try:
            with self._pool.connection() as conn:
                cur = conn.execute("DELETE FROM pipelines WHERE workspace=%s AND id=%s", (workspace, pipeline_id))
                return cur.rowcount > 0
        except pgerr.ForeignKeyViolation:
            raise InUse("this pipeline still has jobs; delete them first") from None

    def add_version(self, workspace, pipeline_id, spec, notes, by):
        now = datetime.now(UTC)
        with self._pool.connection() as conn, conn.transaction():
            # Locking the pipeline row serialises concurrent saves so version numbers are gapless.
            row = conn.execute("SELECT id FROM pipelines WHERE workspace=%s AND id=%s FOR UPDATE", (workspace, pipeline_id)).fetchone()
            if not row:
                return None
            n = conn.execute("SELECT COALESCE(max(version),0)+1 AS n FROM pipeline_versions WHERE pipeline_id=%s", (pipeline_id,)).fetchone()["n"]
            conn.execute(
                "INSERT INTO pipeline_versions (pipeline_id, version, spec, notes, created_by, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                (pipeline_id, n, Jsonb(spec.model_dump(by_alias=True, mode="json")), notes, by, now),
            )
            conn.execute("UPDATE pipelines SET updated_at=%s WHERE id=%s", (now, pipeline_id))
        return PipelineVersion(pipeline_id, n, spec, notes, by, now)

    def get_version(self, workspace, pipeline_id, version):
        sql = "SELECT v.* FROM pipeline_versions v JOIN pipelines p ON p.id = v.pipeline_id WHERE p.workspace=%s AND p.id=%s"
        args: list[Any] = [workspace, pipeline_id]
        if version is None:
            sql += " ORDER BY v.version DESC LIMIT 1"
        else:
            sql += " AND v.version=%s"
            args.append(version)
        with self._pool.connection() as conn:
            r = conn.execute(sql, args).fetchone()
        return self._version(r) if r else None

    def list_versions(self, workspace, pipeline_id, limit, offset):
        base = "FROM pipeline_versions v JOIN pipelines p ON p.id = v.pipeline_id WHERE p.workspace=%s AND p.id=%s"
        with self._pool.connection() as conn:
            total = conn.execute(f"SELECT count(*) AS n {base}", (workspace, pipeline_id)).fetchone()["n"]
            rows = conn.execute(f"SELECT v.* {base} ORDER BY v.version DESC LIMIT %s OFFSET %s", (workspace, pipeline_id, limit, offset)).fetchall()
        return Page([self._version(r) for r in rows], total)

    # ---- jobs ----
    @staticmethod
    def _job_args(j: Job) -> tuple[Any, ...]:
        return (
            j.name,
            j.pipeline_id,
            j.pipeline_version,
            Jsonb(j.schedule.model_dump(by_alias=True)) if j.schedule else None,
            Jsonb(j.retry.model_dump(by_alias=True)) if j.retry else None,
            j.allow_concurrent_runs,
            j.next_run_at,
            j.owner_sub,
            j.role_ceiling,
        )

    def create_job(self, job):
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    "INSERT INTO jobs (id, workspace, created_by, created_at, updated_at, name, pipeline_id, pipeline_version, schedule, retry, allow_concurrent_runs, next_run_at, owner_sub, role_ceiling)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (job.id, job.workspace, job.created_by, job.created_at, job.updated_at, *self._job_args(job)),
                )
        except pgerr.UniqueViolation:
            raise Conflict("a job with that name already exists in this workspace", "name") from None
        return job

    def get_job(self, workspace, job_id):
        with self._pool.connection() as conn:
            r = conn.execute("SELECT * FROM jobs WHERE workspace=%s AND id=%s", (workspace, job_id)).fetchone()
        return self._job(r) if r else None

    def list_jobs(self, workspace, pipeline_id, q, limit, offset):
        where, args = "workspace = %s", [workspace]
        if pipeline_id is not None:
            where += " AND pipeline_id = %s"
            args.append(pipeline_id)
        if q.strip():
            where += " AND lower(name) LIKE %s ESCAPE '\\'"
            args.append(_like(q))
        with self._pool.connection() as conn:
            total = conn.execute(f"SELECT count(*) AS n FROM jobs WHERE {where}", args).fetchone()["n"]
            rows = conn.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY lower(name), id LIMIT %s OFFSET %s", [*args, limit, offset]).fetchall()
        return Page([self._job(r) for r in rows], total)

    def update_job(self, job):
        try:
            with self._pool.connection() as conn:
                cur = conn.execute(
                    "UPDATE jobs SET name=%s, pipeline_id=%s, pipeline_version=%s, schedule=%s, retry=%s, allow_concurrent_runs=%s, next_run_at=%s, owner_sub=%s, role_ceiling=%s, updated_at=%s"
                    " WHERE workspace=%s AND id=%s",
                    (*self._job_args(job), job.updated_at, job.workspace, job.id),
                )
                if cur.rowcount == 0:
                    return None
        except pgerr.UniqueViolation:
            raise Conflict("a job with that name already exists in this workspace", "name") from None
        return job

    def delete_job(self, workspace, job_id):
        with self._pool.connection() as conn:
            return conn.execute("DELETE FROM jobs WHERE workspace=%s AND id=%s", (workspace, job_id)).rowcount > 0

    def claim_due_jobs(self, now, limit):
        out: list[Job] = []
        with self._pool.connection() as conn, conn.transaction():
            # SKIP LOCKED: replicas polling at once each take a disjoint set, so a due fire is
            # claimed exactly once; the advance to next_run_at commits with the claim.
            rows = conn.execute(
                "SELECT * FROM jobs WHERE next_run_at IS NOT NULL AND next_run_at <= %s AND schedule IS NOT NULL"
                " AND (schedule->>'enabled')::boolean ORDER BY next_run_at, id LIMIT %s FOR UPDATE SKIP LOCKED",
                (now, limit),
            ).fetchall()
            for r in rows:
                job = self._job(r)
                out.append(job)
                nxt = next_fire_trigger(job.schedule, now)  # type: ignore[arg-type]
                conn.execute("UPDATE jobs SET next_run_at=%s WHERE id=%s", (nxt, job.id))
        return out

    def list_upcoming(self, limit):
        # A plain read — no FOR UPDATE, no transaction, no advance. Safe (and cheap) for every
        # replica to run on a coarse cadence; only claim_due_jobs above is the locking write.
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE next_run_at IS NOT NULL AND schedule IS NOT NULL"
                " AND (schedule->>'enabled')::boolean ORDER BY next_run_at, id LIMIT %s",
                (limit,),
            ).fetchall()
        return [self._job(r) for r in rows]

    # ---- runs ----
    def create_run(self, run, task_keys, exclusive=False):
        with self._pool.connection() as conn, conn.transaction():
            if exclusive:
                # Lock the job row: concurrent exclusive triggers queue up here, so the count below
                # is seen by the next one only after this insert commits.
                conn.execute("SELECT id FROM jobs WHERE workspace=%s AND id=%s FOR UPDATE", (run.workspace, run.job_id))
                n = conn.execute(
                    "SELECT count(*) AS n FROM runs WHERE workspace=%s AND job_id=%s AND status = ANY(%s)", (run.workspace, run.job_id, list(RUN_ACTIVE))
                ).fetchone()["n"]
                if n:
                    raise Busy("this job already has an active run")
            conn.execute(
                "INSERT INTO runs (id, workspace, job_id, pipeline_id, pipeline_version, status, trigger, triggered_by, created_at, owner_sub)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (run.id, run.workspace, run.job_id, run.pipeline_id, run.pipeline_version, run.status, run.trigger, run.triggered_by, run.created_at, run.owner_sub),
            )
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO task_runs (run_id, task_key, status) VALUES (%s,%s,%s)", [(run.id, k, TASK_PENDING) for k in task_keys]
                )
        return run

    def get_run(self, workspace, run_id):
        with self._pool.connection() as conn:
            r = conn.execute("SELECT * FROM runs WHERE workspace=%s AND id=%s", (workspace, run_id)).fetchone()
        return self._run(r) if r else None

    def list_runs(self, workspace, job_id, status, limit, offset):
        where, args = "workspace = %s", [workspace]
        if job_id is not None:
            where += " AND job_id = %s"
            args.append(job_id)
        if status is not None:
            where += " AND status = %s"
            args.append(status)
        with self._pool.connection() as conn:
            total = conn.execute(f"SELECT count(*) AS n FROM runs WHERE {where}", args).fetchone()["n"]
            rows = conn.execute(f"SELECT * FROM runs WHERE {where} ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s", [*args, limit, offset]).fetchall()
        return Page([self._run(r) for r in rows], total)

    def count_active_runs(self, workspace, job_id):
        with self._pool.connection() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM runs WHERE workspace=%s AND job_id=%s AND status = ANY(%s)", (workspace, job_id, list(RUN_ACTIVE))
            ).fetchone()["n"]

    def mark_running(self, run_id, worker_id, now):
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE runs SET status=%s, started_at=%s, worker_id=%s, heartbeat_at=%s WHERE id=%s AND status=%s",
                (RUN_RUNNING, now, worker_id, now, run_id, RUN_QUEUED),
            )

    def heartbeat(self, run_id, now):
        with self._pool.connection() as conn:
            conn.execute("UPDATE runs SET heartbeat_at=%s WHERE id=%s AND status = ANY(%s)", (now, run_id, list(RUN_ACTIVE)))

    def finish_run(self, run_id, status, error, now):
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE runs SET status=%s, error=%s, finished_at=%s WHERE id=%s AND status <> ALL(%s)", (status, error, now, run_id, list(RUN_TERMINAL))
            )
            return cur.rowcount > 0

    def request_cancel(self, workspace, run_id):
        with self._pool.connection() as conn:
            conn.execute("UPDATE runs SET cancel_requested=true WHERE workspace=%s AND id=%s AND status = ANY(%s)", (workspace, run_id, list(RUN_ACTIVE)))
        return self.get_run(workspace, run_id)

    def is_cancel_requested(self, run_id):
        with self._pool.connection() as conn:
            r = conn.execute("SELECT cancel_requested FROM runs WHERE id=%s", (run_id,)).fetchone()
        return bool(r and r["cancel_requested"])

    def stale_runs(self, heartbeat_before, queued_before):
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE (status=%s AND heartbeat_at < %s) OR (status=%s AND created_at < %s)",
                (RUN_RUNNING, heartbeat_before, RUN_QUEUED, queued_before),
            ).fetchall()
        return [self._run(r) for r in rows]

    def list_task_runs(self, run_id):
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT * FROM task_runs WHERE run_id=%s ORDER BY task_key", (run_id,)).fetchall()
        return [TaskRun(r["run_id"], r["task_key"], r["status"], r["attempts"], r["started_at"], r["finished_at"], r["error"]) for r in rows]

    def update_task_run(self, t):
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE task_runs SET status=%s, attempts=%s, started_at=%s, finished_at=%s, error=%s WHERE run_id=%s AND task_key=%s",
                (t.status, t.attempts, t.started_at, t.finished_at, t.error, t.run_id, t.task_key),
            )

    # ---- logs ----
    def append_logs(self, lines):
        if not lines:
            return
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO run_logs (run_id, seq, ts, task_key, attempt, stream, message) VALUES (%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (run_id, seq) DO NOTHING",
                [(ln.run_id, ln.seq, ln.ts, ln.task_key, ln.attempt, ln.stream, _pg_text(ln.message)) for ln in lines],
            )

    def list_logs(self, run_id, task_key, after_seq, limit):
        sql, args = "SELECT * FROM run_logs WHERE run_id=%s AND seq > %s", [run_id, after_seq]
        if task_key is not None:
            sql += " AND task_key = %s"
            args.append(task_key)
        with self._pool.connection() as conn:
            rows = conn.execute(sql + " ORDER BY seq LIMIT %s", [*args, limit]).fetchall()
        return [LogLine(r["run_id"], r["seq"], r["ts"], r["task_key"], r["attempt"], r["stream"], r["message"]) for r in rows]


def _pg_text(s: str) -> str:
    """Postgres text cannot hold NUL. Task output is arbitrary, so scrub rather than lose the
    whole batch of log lines to one stray byte."""
    return s.replace("\x00", "�")

