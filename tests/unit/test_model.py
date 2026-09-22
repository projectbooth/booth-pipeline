"""The DAG rules the brief states (source -> transform(s) -> sink) and the spec's wire format."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

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
        ([task("a", "source"), task("b", "transform", ["a", "a"])], "same dependency twice", "tasks[1].dependsOn"),
    ],
)
def test_structural_rules_name_the_offending_field(tasks, message, field):
    with pytest.raises(ModelError, match=message) as ei:
        validate_structure(spec(*tasks))
    assert ei.value.field == field


def test_kind_is_optional_and_purely_decorative_never_a_dependency_constraint():
    """ADR 0062: kind is a label, not a topology constraint. Each of these was a hard 422 before —
    a "source" with dependencies, a "sink"/"transform" with none, a "sink" with downstream tasks,
    and a completely untagged task — and every one is now simply valid."""
    validate_structure(spec(task("a", "source", ["b"]), task("b", "source")))  # a "source" WITH deps
    validate_structure(spec(task("a", "source"), task("b", "transform")))  # a "transform" with none
    validate_structure(spec(task("a", "source"), task("b", "sink")))  # a "sink" with none
    validate_structure(spec(task("a", "source"), task("b", "sink", ["a"]), task("c", "transform", ["b"])))  # sink with downstream
    validate_structure(spec({"key": "a", "code": {"type": "inline", "source": "x = 1"}}))  # no kind at all
    assert spec({"key": "a", "code": {"type": "inline", "source": "x = 1"}}).tasks[0].kind is None


def test_kind_none_round_trips_and_an_old_saved_pipeline_with_kind_set_stays_valid():
    bare = PipelineSpec.model_validate({"tasks": [{"key": "a", "code": {"type": "inline", "source": "x = 1"}}]})
    assert bare.tasks[0].kind is None
    dumped = bare.model_dump(by_alias=True)
    assert dumped["tasks"][0]["kind"] is None
    assert PipelineSpec.model_validate(dumped) == bare
    # a pipeline saved under the old, stricter rules is unaffected: still valid, kind preserved
    tagged = spec(task("a", "source"), task("b", "sink", ["a"]))
    validate_structure(tagged)
    assert [t.kind for t in tagged.tasks] == ["source", "sink"]


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


# ---- Trigger union (ADR 0065) -----------------------------------------------------------------


def test_a_bare_cron_shaped_dict_with_no_type_still_loads_as_a_crontrigger():
    """Back-compat: every trigger saved before ADR 0065 is a bare {cron, timezone, enabled} object
    with no discriminator. This is the exact shape a pre-existing database row still has."""
    from pydantic import TypeAdapter

    from booth_pipeline.model import CronTrigger, Trigger

    adapter = TypeAdapter(Trigger)
    t = adapter.validate_python({"cron": "0 9 * * *", "timezone": "America/Toronto", "enabled": True})
    assert isinstance(t, CronTrigger)
    assert (t.type, t.cron, t.timezone) == ("cron", "0 9 * * *", "America/Toronto")
    # a value that already has an explicit type is untouched by the coercion
    t2 = adapter.validate_python({"type": "cron", "cron": "* * * * *", "timezone": "UTC", "enabled": False})
    assert t2 == CronTrigger(cron="* * * * *", enabled=False)


def test_interval_trigger_shape_and_bounds():
    from pydantic import TypeAdapter

    from booth_pipeline.model import MAX_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS, IntervalTrigger, Trigger

    t = IntervalTrigger(seconds=30)
    assert (t.type, t.seconds, t.enabled) == ("interval", 30, True)
    IntervalTrigger(seconds=MIN_INTERVAL_SECONDS)
    IntervalTrigger(seconds=MAX_INTERVAL_SECONDS)
    for bad in (MIN_INTERVAL_SECONDS - 1, 0, -5, MAX_INTERVAL_SECONDS + 1):
        with pytest.raises(ValidationError):
            IntervalTrigger(seconds=bad)
    # and it round-trips through the discriminated union exactly like the cron member does
    adapter = TypeAdapter(Trigger)
    assert adapter.validate_python({"type": "interval", "seconds": 45, "enabled": True}) == IntervalTrigger(seconds=45)


def test_an_unknown_trigger_type_is_rejected_not_silently_ignored():
    from pydantic import TypeAdapter

    from booth_pipeline.model import Trigger

    with pytest.raises(ValidationError):
        TypeAdapter(Trigger).validate_python({"type": "webhook", "url": "http://example.com"})


def test_trigger_is_optional_on_a_job_and_none_passes_through():
    from booth_pipeline.schemas import JobInput

    assert JobInput(name="j", pipeline_id="p").schedule is None
    assert JobInput(name="j", pipeline_id="p", schedule=None).schedule is None


def test_next_fire_trigger_dispatches_cron_unchanged_and_interval_by_simple_addition():
    from datetime import timedelta

    from booth_pipeline.model import IntervalTrigger
    from booth_pipeline.schedule import next_fire, next_fire_trigger

    now = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    cron = Schedule(cron="*/15 * * * *", timezone="UTC")
    assert next_fire_trigger(cron, now) == next_fire(cron.cron, cron.timezone, now)

    interval = IntervalTrigger(seconds=90)
    assert next_fire_trigger(interval, now) == now + timedelta(seconds=90)
    # no calendar alignment: firing again from the fire time just adds the interval again
    fired_at = next_fire_trigger(interval, now)
    assert next_fire_trigger(interval, fired_at) == fired_at + timedelta(seconds=90)


def test_next_fire_trigger_requires_a_timezone_aware_after_for_either_kind():
    from booth_pipeline.model import IntervalTrigger
    from booth_pipeline.schedule import next_fire_trigger

    with pytest.raises(ValueError, match="timezone-aware"):
        next_fire_trigger(IntervalTrigger(seconds=10), datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        next_fire_trigger(Schedule(cron="* * * * *"), datetime(2026, 1, 1))
