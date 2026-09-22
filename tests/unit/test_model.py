"""The DAG rules the brief states (source -> transform(s) -> sink) and the spec's wire format."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from booth_pipeline.model import (
    ModelError,
    PipelineSpec,
    Schedule,
    sha256_text,
    topological_order,
    validate_structure,
)

from .helpers import linear, spec, task


def test_linear_pipeline_is_valid_and_ordered():
    s = linear()
    validate_structure(s)
    assert topological_order(s) == ["extract", "clean", "load"]


def test_diamond_orders_dependencies_first_and_is_deterministic():
    s = spec(
        task("a", "source"),
        task("b", "transform", ["a"]),
        task("c", "transform", ["a"]),
        task("d", "sink", ["b", "c"]),
    )
    validate_structure(s)
    order = topological_order(s)
    assert order == ["a", "b", "c", "d"]
    assert order.index("a") < order.index("b") < order.index("d")


def test_empty_pipeline_is_rejected():
    with pytest.raises(ModelError, match="at least one task"):
        validate_structure(PipelineSpec())


@pytest.mark.parametrize(
    "tasks, message, field",
    [
        ([task("a", "source"), task("a", "source")], "duplicate task key", "tasks[1].key"),
        ([task("a", "source", ["a"])], "cannot depend on itself", "tasks[0].dependsOn"),
        ([task("a", "source"), task("b", "transform", ["nope"])], "not a task in this pipeline", "tasks[1].dependsOn"),
        ([task("a", "source", ["b"]), task("b", "source")], "source 'a' cannot have dependencies", "tasks[0].dependsOn"),
        ([task("a", "source"), task("b", "transform")], "needs at least one upstream", "tasks[1].dependsOn"),
        ([task("a", "source"), task("b", "sink")], "needs at least one upstream", "tasks[1].dependsOn"),
        (
            [task("a", "source"), task("b", "sink", ["a"]), task("c", "transform", ["b"])],
            "sink 'b' cannot have downstream tasks",
            "tasks[1].kind",
        ),
        ([task("a", "source"), task("b", "transform", ["a", "a"])], "same dependency twice", "tasks[1].dependsOn"),
    ],
)
def test_structural_rules_name_the_offending_field(tasks, message, field):
    with pytest.raises(ModelError, match=message) as ei:
        validate_structure(spec(*tasks))
    assert ei.value.field == field


def test_cycle_is_rejected():
    s = spec(task("a", "source"), task("b", "transform", ["a", "c"]), task("c", "transform", ["b"]))
    with pytest.raises(ModelError, match="form a cycle: b, c"):
        validate_structure(s)


def test_unavailable_runner_is_rejected_but_base_is_fine():
    s = spec(task("a", "source", runner="spark"))
    validate_structure(s)  # runner availability is only checked when the caller supplies the set
    with pytest.raises(ModelError, match="runner 'spark' is not available; available: base") as ei:
        validate_structure(s, {"base"})
    assert ei.value.field == "tasks[0].runner"
    validate_structure(spec(task("a", "source")), {"base"})


@pytest.mark.parametrize("key", ["Extract", "1abc", "has space", "a-b", "", "x" * 64, "ünï"])
def test_bad_task_keys_are_rejected(key):
    with pytest.raises(ValidationError):
        spec(task(key, "source"))


def test_unknown_fields_are_rejected_not_ignored():
    # a typo must be loud: "maxRetrys" silently meaning "no retries" would be a nasty surprise
    with pytest.raises(ValidationError):
        spec(task("a", "source", retry={"maxRetrys": 3}))


def test_wire_format_is_camel_case_and_round_trips():
    s = spec(task("a", "source", retry={"maxRetries": 2, "delaySeconds": 1.5, "backoff": "exponential"}, timeoutSeconds=60))
    dumped = s.model_dump(by_alias=True)
    t = dumped["tasks"][0]
    assert t["dependsOn"] == [] and t["timeoutSeconds"] == 60
    assert t["retry"] == {"maxRetries": 2, "delaySeconds": 1.5, "backoff": "exponential"}
    assert PipelineSpec.model_validate(dumped) == s


def test_inline_source_must_be_nonblank_and_bounded():
    with pytest.raises(ValidationError):
        spec(task("a", "source", source="   \n"))
    with pytest.raises(ValidationError):
        spec(task("a", "source", source="x = 1\x00"))
    with pytest.raises(ValidationError):
        spec(task("a", "source", source="#" * (256 * 1024 + 1)))


def test_retry_bounds():
    for bad in ({"maxRetries": 11}, {"maxRetries": -1}, {"delaySeconds": -1}, {"backoff": "sideways"}):
        with pytest.raises(ValidationError):
            spec(task("a", "source", retry=bad))


def test_catalog_reference_shape_is_just_entry_and_version():
    s = spec({"key": "a", "kind": "source", "code": {"type": "catalog", "entryId": "e-1", "version": "latest"}})
    code = s.tasks[0].code
    assert (code.entry_id, code.version) == ("e-1", "latest")
    assert not code.resolved  # nothing has been snapshotted yet


def test_sha256_is_over_utf8_bytes():
    # the same bytes booth-catalog stores, so a digest is comparable across the two modules
    assert sha256_text("héllo") == hashlib.sha256("héllo".encode()).hexdigest()


def test_schedule_validation():
    Schedule(cron="*/5 * * * *", timezone="America/Toronto")
    for bad in ("* * * *", "* * * * * *", "61 * * * *", "@daily", "not cron at all x"):
        with pytest.raises(ValidationError):
            Schedule(cron=bad)
    with pytest.raises(ValidationError):
        Schedule(cron="0 9 * * *", timezone="Mars/Olympus")
