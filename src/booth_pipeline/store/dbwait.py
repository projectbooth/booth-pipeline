"""Retry the database's first connection at startup (ARCHITECTURE.md item 66 (c)).

On the homelab a freshly scheduled pod's first connections to PostgreSQL are refused for a few
seconds (k3s's kube-router is still adding the new pod's IP to the NetworkPolicy allow set), so
the module used to fail its first start and restart. This rides out that window with a bounded
backoff instead. A copy of booth-core's internal/dbwait pattern, not an import of it: 500 ms
doubling to 8 s, 60 s in total, every retry logged, then a loud failure with the last error.

Only connection-level failures are retried (refused, timeout, DNS, "starting up"). A wrong
password, a missing database or an SQL error fails at once: waiting cannot fix them.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TypeVar

import psycopg

log = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_MAX_ELAPSED = 60.0
_INITIAL_BACKOFF = 0.5
_MAX_BACKOFF = 8.0

# psycopg reports connect failures as OperationalError with no sqlstate, the server's FATAL for a
# bad password or missing database included, so the message is all there is to go on. Matched
# lower-cased; anything not listed is treated as permanent.
_TRANSIENT = (
    "connection refused",
    "timeout expired",
    "timed out",
    "failed to resolve host",
    "could not translate host name",
    "name or service not known",
    "temporary failure in name resolution",
    "no route to host",
    "network is unreachable",
    "connection reset",
    "server closed the connection unexpectedly",
    "the database system is starting up",
)


class DatabaseUnavailable(RuntimeError):
    """The database never accepted a connection within the bound."""


def is_transient(err: BaseException) -> bool:
    if isinstance(err, (psycopg.errors.ConnectionTimeout, psycopg.errors.CannotConnectNow)):  # 57P03: starting up
        return True
    if type(err) is not psycopg.OperationalError:  # SQL errors are OperationalError subclasses with a sqlstate
        return False
    msg = str(err).lower()
    return any(s in msg for s in _TRANSIENT)


def retry(
    what: str,
    fn: Callable[[], T],
    *,
    max_elapsed: float = DEFAULT_MAX_ELAPSED,
    cancel: threading.Event | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """Call ``fn`` (one connection attempt) until it succeeds, retrying transient failures with
    exponential backoff until ``max_elapsed`` seconds have passed since the first attempt. A set
    ``cancel`` event stops the wait at once. Raises the permanent error as is, or
    DatabaseUnavailable (chained to the last error) once the bound is spent."""
    cancel = cancel or threading.Event()
    deadline = clock() + max_elapsed
    backoff = _INITIAL_BACKOFF
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except Exception as err:
            if not is_transient(err):
                raise
            if clock() >= deadline:
                raise DatabaseUnavailable(f"{what}: still failing after {max_elapsed:g}s ({attempt} attempts): {err}") from err
            log.warning("%s: not ready yet (attempt %d: %s); retrying in %gs", what, attempt, err, backoff)
            if cancel.wait(backoff):
                raise DatabaseUnavailable(f"{what}: cancelled while waiting ({attempt} attempts): {err}") from err
            backoff = min(backoff * 2, _MAX_BACKOFF)
