"""Workload identity, minting side (ADR 0056/0058): the client, the access provider, and the engine's
handling of a refusal."""

from __future__ import annotations

import logging

import httpx
import pytest

from booth_pipeline.engine import execute
from booth_pipeline.runners.base import Cancellation, TaskAccess, TaskFailed
from booth_pipeline.runners.registry import RunnerRegistry
from booth_pipeline.workload import (
    OWNER_NOT_CURRENT,
    AccessProvider,
    MintNotConfigured,
    MintRefused,
    MintUnavailable,
    WorkloadMinter,
)

from .helpers import ListRecorder, spec, task

CRED = "bwmc.pipeline.SUPER-SECRET-MAC"
GOOD = {"token": "jwt-abc", "tokenType": "Bearer", "expiresAt": "2026-09-21T12:10:00Z", "role": "editor"}


def minter(handler) -> WorkloadMinter:
    return WorkloadMinter(CRED, "http://core.test/api/internal/workload-tokens", "http://core.test", transport=httpx.MockTransport(handler))


# ---- the minting call ------------------------------------------------------------------------


def test_the_mint_request_carries_exactly_what_the_contract_names():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"], seen["url"], seen["body"] = req.headers["authorization"], str(req.url), req.content
        return httpx.Response(200, json=GOOD)

    t = minter(handler).mint("acme", "run:r-1", "editor", "user-sub-42")
    assert seen["auth"] == f"Bearer {CRED}"  # authenticated with the minting credential, nothing else
    assert seen["url"] == "http://core.test/api/internal/workload-tokens"
    import json

    assert json.loads(seen["body"]) == {"workspace": "acme", "subject": "run:r-1", "roleCeiling": "editor", "owner": "user-sub-42"}
    assert (t.token, t.role) == ("jwt-abc", "editor") and t.expires_at.tzinfo is not None


def test_the_role_actually_granted_is_reported_even_when_below_the_ceiling():
    t = minter(lambda r: httpx.Response(200, json={**GOOD, "role": "viewer"})).mint("acme", "run:r", "editor", "u")
    assert t.role == "viewer"


def test_a_403_is_an_expected_refusal_with_a_message_a_person_can_act_on():
    with pytest.raises(MintRefused) as ei:
        minter(lambda r: httpx.Response(403, json={"error": "forbidden"})).mint("acme", "run:r", "editor", "u")
    msg = str(ei.value)
    assert msg == OWNER_NOT_CURRENT
    assert "signed in" in msg and "recently enough" in msg and "take it over" in msg


def test_a_401_blames_the_operators_credential_not_the_owner():
    with pytest.raises(MintRefused) as ei:
        minter(lambda r: httpx.Response(401, json={"error": "no"})).mint("acme", "run:r", "editor", "u")
    assert "credential" in str(ei.value) and "signed in" not in str(ei.value)


@pytest.mark.parametrize("resp", [httpx.Response(503, json={}), httpx.Response(500, text="x"), httpx.Response(400, json={"error": "bad"}), httpx.Response(200, json={"nope": 1}), httpx.Response(200, text="not json")])
def test_anything_else_is_unavailable_and_therefore_retryable(resp):
    with pytest.raises(MintUnavailable):
        minter(lambda r: resp).mint("acme", "run:r", "editor", "u")


def test_an_unreachable_core_is_unavailable_not_a_crash():
    def boom(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(MintUnavailable, match="unreachable"):
        minter(boom).mint("acme", "run:r", "editor", "u")


def test_neither_the_credential_nor_a_token_is_ever_logged(caplog):
    caplog.set_level(logging.DEBUG)
    minter(lambda r: httpx.Response(200, json=GOOD)).mint("acme", "run:r", "editor", "u")
    with pytest.raises(MintRefused):
        minter(lambda r: httpx.Response(403, json={})).mint("acme", "run:r", "editor", "u")
    assert CRED not in caplog.text and "jwt-abc" not in caplog.text


def test_the_secret_is_read_from_the_files_core_writes(tmp_path):
    (tmp_path / "credential").write_text(CRED + "\n")
    (tmp_path / "url").write_text("http://core.svc/api/internal/workload-tokens\n")
    (tmp_path / "issuer").write_text("http://core.svc\n")
    m = WorkloadMinter.from_dir(str(tmp_path))
    assert m is not None and m.issuer == "http://core.svc"
    assert WorkloadMinter.from_dir(str(tmp_path / "missing")) is None  # module not given the Secret
    (tmp_path / "url").write_text("")
    assert WorkloadMinter.from_dir(str(tmp_path)) is None  # an incomplete Secret is treated as absent


# ---- the access provider ---------------------------------------------------------------------


def provider(m, owner="u-1", ceiling="editor") -> AccessProvider:
    return AccessProvider(m, workspace="acme", subject="run:r1", role_ceiling=ceiling, owner=owner, storage_url="http://c/modules/storage", catalog_url="http://c/modules/catalog")


def test_grant_hands_over_a_token_and_where_to_send_it_and_can_refresh():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json={**GOOD, "token": f"jwt-{len(calls)}"})

    access = provider(minter(handler)).grant()
    assert isinstance(access, TaskAccess)
    assert (access.workspace, access.token, access.storage_url, access.catalog_url) == ("acme", "jwt-1", "http://c/modules/storage", "http://c/modules/catalog")
    assert access.refresh is not None and access.refresh() == "jwt-2"  # a fresh mint, not a cached copy


def test_without_a_credential_or_an_owner_a_grant_is_refused_clearly():
    with pytest.raises(MintNotConfigured, match="no workload-identity credential"):
        provider(None).grant()
    with pytest.raises(MintRefused, match="no recorded owner"):
        provider(minter(lambda r: httpx.Response(200, json=GOOD)), owner="").grant()


# ---- the engine ------------------------------------------------------------------------------


class Spy:
    """A runner that records the access each invocation was handed."""

    id = "base"

    def __init__(self) -> None:
        self.access: list[TaskAccess | None] = []

    def run(self, inv, log, cancel):
        self.access.append(inv.access)
        return "ok"


def run_engine(tasks, m, retry=None):
    spy, rec = Spy(), ListRecorder()
    res = execute(spec(*tasks), run_id="r1", registry=RunnerRegistry([spy]), recorder=rec, cancel=Cancellation(), access=provider(m))
    return res, rec, spy


def test_a_task_that_did_not_opt_in_never_triggers_a_mint():
    """The point of the per-task opt-in: a pipeline that never touches storage cannot be blocked by
    a lapsed owner, or hand out an identity it does not use."""
    mints = []
    res, _, spy = run_engine([task("a", "source"), task("b", "sink", ["a"])], minter(lambda r: mints.append(1) or httpx.Response(200, json=GOOD)))
    assert res.success and mints == [] and spy.access == [None, None]


def test_an_opted_in_task_gets_a_token_and_only_that_task_does():
    res, _, spy = run_engine(
        [task("a", "source", platformAccess=True), task("b", "sink", ["a"])], minter(lambda r: httpx.Response(200, json=GOOD))
    )
    assert res.success
    assert spy.access[0] is not None and spy.access[0].token == "jwt-abc" and spy.access[1] is None


def test_every_attempt_of_a_retried_task_starts_with_a_freshly_minted_token():
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(200, json={**GOOD, "token": f"jwt-{len(n)}"})

    class Flaky(Spy):
        def run(self, inv, log, cancel):
            self.access.append(inv.access)
            if inv.attempt < 3:
                raise TaskFailed("flaky")
            return "ok"

    spy, rec = Flaky(), ListRecorder()
    res = execute(spec(task("a", "source", platformAccess=True, retry={"maxRetries": 2})), run_id="r", registry=RunnerRegistry([spy]), recorder=rec, cancel=Cancellation(), access=provider(minter(handler)))
    assert res.success
    assert [a.token for a in spy.access if a] == ["jwt-1", "jwt-2", "jwt-3"]


def test_a_refused_mint_fails_the_task_plainly_and_is_never_retried():
    mints = []

    def handler(req):
        mints.append(1)
        return httpx.Response(403, json={"error": "forbidden"})

    res, rec, spy = run_engine([task("a", "source", platformAccess=True, retry={"maxRetries": 5}), task("b", "sink", ["a"])], minter(handler))
    assert not res.success and not res.canceled
    assert len(mints) == 1  # five retries configured; a refusal will not change in seconds, so none happen
    assert spy.access == []  # the task's code never ran
    assert rec.statuses("a") == ["started", "failed"] and rec.statuses("b") == []
    failed = next(e for e in rec.events if e[0] == "failed")
    assert "not given platform access" in failed[3] and "signed in" in failed[3]
    # and it is in the RUN's own error, not just the task log
    assert "signed in" in (res.error or "") and "a:" in (res.error or "")


def test_a_transient_mint_failure_is_retried_like_any_other_task_failure():
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(503, json={}) if len(n) < 3 else httpx.Response(200, json=GOOD)

    spy, rec = Spy(), ListRecorder()
    res = execute(spec(task("a", "source", platformAccess=True, retry={"maxRetries": 3})), run_id="r", registry=RunnerRegistry([spy]), recorder=rec, cancel=Cancellation(), access=provider(minter(handler)))
    assert res.success and len(n) == 3


def test_a_deployment_without_workload_identity_says_so_instead_of_crashing():
    spy, rec = Spy(), ListRecorder()
    res = execute(spec(task("a", "source", platformAccess=True)), run_id="r", registry=RunnerRegistry([spy]), recorder=rec, cancel=Cancellation(), access=provider(None))
    assert not res.success and "no workload-identity credential" in (res.error or "")
