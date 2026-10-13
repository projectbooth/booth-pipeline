"""ARCHITECTURE.md item 66 (c): the startup wait for the database's first connection."""

from __future__ import annotations

import logging
import os
import threading
import time

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from booth_pipeline.store import dbwait

REFUSED = psycopg.OperationalError('connection failed: connection to server at "10.43.0.5", port 5432 failed: Connection refused')


class FakeClock:
    """Time that only moves when the retry loop sleeps, so backoff is checked without real waits."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now


class SleepingEvent(threading.Event):
    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self.clock = clock

    def wait(self, timeout: float | None = None) -> bool:
        self.clock.sleeps.append(timeout)
        self.clock.now += timeout
        return self.is_set()


def failing(n: int, err: Exception = REFUSED):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= n:
            raise err
        return "connected"

    return fn, calls


def test_fails_n_times_then_succeeds_with_doubling_backoff_and_a_log_line_per_retry(caplog):
    clock = FakeClock()
    fn, calls = failing(6)
    with caplog.at_level(logging.WARNING, logger="booth_pipeline.store.dbwait"):
        assert dbwait.retry("connecting to PostgreSQL", fn, cancel=SleepingEvent(clock), clock=clock) == "connected"
    assert calls["n"] == 7
    assert clock.sleeps == [0.5, 1, 2, 4, 8, 8]  # 500 ms doubling, capped at 8 s
    retries = [r.getMessage() for r in caplog.records]
    assert len(retries) == 6
    assert retries[0] == "connecting to PostgreSQL: not ready yet (attempt 1: " + str(REFUSED) + "); retrying in 0.5s"


def test_never_succeeds_fails_loudly_with_the_last_error_and_attempt_count():
    clock = FakeClock()
    fn, calls = failing(10**6)
    with pytest.raises(dbwait.DatabaseUnavailable, match=r"still failing after 10s \(6 attempts\): .*Connection refused") as e:
        dbwait.retry("connecting to PostgreSQL", fn, max_elapsed=10, cancel=SleepingEvent(clock), clock=clock)
    assert e.value.__cause__ is REFUSED
    assert calls["n"] == 6  # attempt 5 fails at 7.5 s (< 10, so one more 8 s wait); attempt 6 at 15.5 s


def test_the_default_bound_is_sixty_seconds():
    clock = FakeClock()
    with pytest.raises(dbwait.DatabaseUnavailable, match="after 60s"):
        dbwait.retry("x", failing(10**6)[0], cancel=SleepingEvent(clock), clock=clock)
    assert 60 <= sum(clock.sleeps) < 68


@pytest.mark.parametrize(
    "err",
    [
        psycopg.OperationalError('connection failed: ... failed: FATAL:  password authentication failed for user "booth"'),
        psycopg.OperationalError('connection failed: ... failed: FATAL:  database "booth_mod_pipeline" does not exist'),
        psycopg.errors.UndefinedTable("relation pipelines does not exist"),
        ValueError("not a database error"),
    ],
)
def test_a_permanent_error_fails_at_once(err):
    clock = FakeClock()
    fn, calls = failing(1, err)
    with pytest.raises(type(err)):
        dbwait.retry("x", fn, cancel=SleepingEvent(clock), clock=clock)
    assert calls["n"] == 1 and clock.sleeps == []


def test_cancelled_mid_wait_stops_at_once():
    cancel = threading.Event()
    fn, calls = failing(10**6)
    threading.Timer(0.1, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(dbwait.DatabaseUnavailable, match=r"cancelled while waiting \(1 attempts\)"):
        dbwait.retry("x", fn, cancel=cancel)  # a real clock: the first 0.5 s wait is cut short
    assert calls["n"] == 1 and time.monotonic() - started < 0.4


@pytest.mark.parametrize(
    "msg",
    [
        "connection to server at ..., port 5432 failed: Connection refused",
        "failed to resolve host 'booth-postgresql': [Errno -2] Name or service not known",
        'could not translate host name "pg" to address: Temporary failure in name resolution',
        "connection to server at ... failed: FATAL:  the database system is starting up",
    ],
)
def test_connection_level_failures_are_transient(msg):
    assert dbwait.is_transient(psycopg.OperationalError(msg))
    assert dbwait.is_transient(psycopg.errors.ConnectionTimeout("connection timeout expired"))


def test_real_psycopg_errors_are_classified_correctly():
    """The messages above are what psycopg really says: check against the test database."""
    dsn = os.environ.get("BOOTH_TEST_POSTGRES_DSN")
    if not dsn:
        if os.environ.get("BOOTH_TEST_REQUIRE_EMULATORS") == "1":
            pytest.fail("BOOTH_TEST_POSTGRES_DSN is not set but BOOTH_TEST_REQUIRE_EMULATORS=1")
        pytest.skip("no test database")
    info = conninfo_to_dict(dsn)

    def error(**override) -> Exception:
        try:
            psycopg.connect(make_conninfo(**{**info, **override}), connect_timeout=3).close()
        except psycopg.Error as e:
            return e
        raise AssertionError(f"connected with {override}")

    assert dbwait.is_transient(error(port="1"))  # nothing listens: refused (or a timeout)
    assert not dbwait.is_transient(error(password="wrong"))
    assert not dbwait.is_transient(error(dbname="no_such_database"))
