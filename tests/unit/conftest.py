from __future__ import annotations

import os

import pytest

from booth_pipeline.store.memory import MemoryStore

TEST_DSN_ENV = "BOOTH_TEST_POSTGRES_DSN"


def postgres_dsn() -> str | None:
    dsn = os.environ.get(TEST_DSN_ENV)
    if dsn:
        return dsn
    if os.environ.get("BOOTH_TEST_REQUIRE_EMULATORS") == "1":
        # CI sets this: a missing database must FAIL the build, never silently skip its tests green.
        pytest.fail(f"{TEST_DSN_ENV} is not set but BOOTH_TEST_REQUIRE_EMULATORS=1")
    return None


@pytest.fixture(scope="session")
def pg_store():
    dsn = postgres_dsn()
    if dsn is None:
        pytest.skip(f"{TEST_DSN_ENV} not set; start hack/docker-compose.test.yml (CI always does)")
    from booth_pipeline.store.postgres import PostgresStore

    s = PostgresStore(dsn)
    s.migrate()
    yield s
    s.close()


@pytest.fixture(params=["memory", "postgres"])
def store(request):
    """Every store test runs against both implementations — that is the contract."""
    if request.param == "memory":
        yield MemoryStore()
        return
    s = request.getfixturevalue("pg_store")
    with s._pool.connection() as conn:
        conn.execute("TRUNCATE run_logs, task_runs, runs, jobs, pipeline_versions, pipelines")
    yield s
