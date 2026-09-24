"""The DAG rules (ADR 0071: a pipeline's nodes are references to standalone, versioned tasks) and
the spec's wire format."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from booth_pipeline.model import (
    ModelError,
    PipelineSpec,
    Schedule,
    TaskConfig,
    TaskRef,
    sha256_text,
    topological_order,
    validate_structure,
    validate_task_config,
)

from .helpers import linear, spec, task_config, task_ref


def test_linear_pipeline_is_valid_and_ordered():
    s = linear()
    validate_structure(s)
    assert topological_order(s) == ["extract", "clean", "load"]


def test_diamond_orders_dependencies_first_and_is_deterministic():
    s = spec(
        task_ref("a"),
        task_ref("b", deps=["a"]),
        task_ref("c", deps=["a"]),
        task_ref("d", deps=["b", "c"]),
    )
    validate_structure(s)
    order = topological_order(s)
    assert order == ["a", "b", "c", "d"]
    assert order.index("a") < order.index("b") < order.index("d")


def test_empty_pipeline_is_rejected():
    with pytest.raises(ModelError, match="at least one task"):
        validate_structure(PipelineSpec())


@pytest.mark.parametrize(
    "refs, message, field",
    [
        ([task_ref("a"), task_ref("a")], "duplicate task key", "tasks[1].key"),
        ([task_ref("a", deps=["a"])], "cannot depend on itself", "tasks[0].dependsOn"),
        ([task_ref("a"), task_ref("b", deps=["nope"])], "not a task in this pipeline", "tasks[1].dependsOn"),
        ([task_ref("a"), task_ref("b", deps=["a", "a"])], "same dependency twice", "tasks[1].dependsOn"),
    ],
)
def test_structural_rules_name_the_offending_field(refs, message, field):
    with pytest.raises(ModelError, match=message) as ei:
        validate_structure(spec(*refs))
    assert ei.value.field == field


def test_cycle_is_rejected():
    s = spec(task_ref("a"), task_ref("b", deps=["a", "c"]), task_ref("c", deps=["b"]))
    with pytest.raises(ModelError, match="form a cycle: b, c"):
        validate_structure(s)


def test_validate_structure_does_not_check_that_a_referenced_task_exists_or_is_runnable():
    """ADR 0071: both moved to the referenced Task's own save-time validation. A pipeline's own
    structure check is purely about the DAG shape — it doesn't (can't, without I/O) know or care
    whether "nonexistent-task-id" is real."""
    validate_structure(spec(task_ref("a", task_id="nonexistent-task-id", version=999)))


@pytest.mark.parametrize("key", ["Extract", "1abc", "has space", "a-b", "", "x" * 64, "ünï"])
def test_bad_ref_keys_are_rejected(key):
    with pytest.raises(ValidationError):
        spec(task_ref(key))


def test_unknown_fields_are_rejected_not_ignored():
    # a typo must be loud: "dependsOnn" silently meaning "no dependencies" would be a nasty surprise
    with pytest.raises(ValidationError):
        spec({"key": "a", "taskId": "t1", "taskVersion": 1, "dependsOnn": []})


def test_wire_format_is_camel_case_and_round_trips():
    s = spec(task_ref("a", task_id="t1", version=2, deps=[]))
    dumped = s.model_dump(by_alias=True)
    t = dumped["tasks"][0]
    assert t["taskId"] == "t1" and t["taskVersion"] == 2 and t["dependsOn"] == []
    assert PipelineSpec.model_validate(dumped) == s


# ---- TaskRef: references a standalone task's version (ADR 0071) -------------------------------


def test_task_ref_version_defaults_to_latest_and_resolves_once_pinned():
    ref = TaskRef.model_validate({"key": "a", "taskId": "t1"})
    assert ref.task_version == "latest"
    assert not ref.resolved
    pinned = TaskRef.model_validate({"key": "a", "taskId": "t1", "taskVersion": 3})
    assert pinned.task_version == 3
    assert pinned.resolved


def test_task_ref_position_and_depends_on_are_the_references_own_wiring():
    ref = TaskRef.model_validate({"key": "a", "taskId": "t1", "position": {"x": 10.5, "y": -3}, "dependsOn": ["b"]})
    assert (ref.position.x, ref.position.y) == (10.5, -3)
    assert ref.depends_on == ["b"]


def test_task_ref_has_no_kind_field_at_all():
    """ADR 0071 removes `kind` entirely (ADR 0062 had already made it decorative) — a value sent
    for it is simply an unrecognised field, same as any other typo."""
    with pytest.raises(ValidationError):
        TaskRef.model_validate({"key": "a", "taskId": "t1", "kind": "source"})


# ---- TaskConfig: a standalone task's own versioned config (ADR 0071) ---------------------------


def test_task_config_has_no_key_depends_on_position_or_kind():
    """Those describe how a TaskRef is wired into one pipeline's DAG, not anything about the Task
    itself — a TaskConfig is pure "what runs", independent of any pipeline."""
    for bad_field in ("key", "dependsOn", "position", "kind"):
        with pytest.raises(ValidationError):
            TaskConfig.model_validate({"code": {"type": "inline", "source": "x = 1"}, bad_field: "x"})


def test_task_config_wire_format_is_camel_case_and_round_trips():
    c = TaskConfig.model_validate(task_config(retry={"maxRetries": 2, "delaySeconds": 1.5, "backoff": "exponential"}, timeoutSeconds=60))
    dumped = c.model_dump(by_alias=True)
    assert dumped["timeoutSeconds"] == 60
    assert dumped["retry"] == {"maxRetries": 2, "delaySeconds": 1.5, "backoff": "exponential"}
    assert TaskConfig.model_validate(dumped) == c


def test_task_config_retry_bounds():
    for bad in ({"maxRetries": 11}, {"maxRetries": -1}, {"delaySeconds": -1}, {"backoff": "sideways"}):
        with pytest.raises(ValidationError):
            TaskConfig.model_validate(task_config(retry=bad))


def test_task_config_params_must_be_json_serialisable_and_bounded():
    with pytest.raises(ValidationError):
        TaskConfig.model_validate(task_config(params={"n": {1, 2, 3}}))  # a set: not JSON-serialisable
    with pytest.raises(ValidationError):
        TaskConfig.model_validate(task_config(params={"blob": "x" * (64 * 1024 + 1)}))


def test_validate_task_config_checks_runner_availability_not_validate_structure():
    """ADR 0071 moves this off the pipeline entirely: it is the referenced Task's own concern,
    checked when that task's version is saved — validate_structure (above) never touches it."""
    c = TaskConfig.model_validate(task_config(runner="spark"))
    validate_task_config(c)  # availability is only checked when the caller supplies the set
    with pytest.raises(ModelError, match="runner 'spark' is not available; available: base") as ei:
        validate_task_config(c, {"base"})
    assert ei.value.field == "runner"
    validate_task_config(TaskConfig.model_validate(task_config()), {"base"})


def test_validate_task_config_bounds_a_resolved_snapshots_own_source_size():
    from booth_pipeline.model import MAX_TASK_SOURCE_BYTES

    huge = TaskConfig(code={"type": "catalog", "entryId": "e", "version": "1", "source": "x" * (MAX_TASK_SOURCE_BYTES + 1)})
    with pytest.raises(ModelError, match="more than"):
        validate_task_config(huge)
    # an UNRESOLVED reference (no source snapshotted yet) has nothing to bound
    validate_task_config(TaskConfig(code={"type": "catalog", "entryId": "e", "version": "1"}))


def test_inline_source_must_be_nonblank_and_bounded():
    with pytest.raises(ValidationError):
        TaskConfig.model_validate(task_config(source="   \n"))
    with pytest.raises(ValidationError):
        TaskConfig.model_validate(task_config(source="x = 1\x00"))
    with pytest.raises(ValidationError):
        TaskConfig.model_validate(task_config(source="#" * (256 * 1024 + 1)))


def test_catalog_reference_shape_is_just_entry_and_version():
    c = TaskConfig.model_validate({"code": {"type": "catalog", "entryId": "e-1", "version": "latest"}})
    assert (c.code.entry_id, c.code.version) == ("e-1", "latest")
    assert not c.code.resolved  # nothing has been snapshotted yet


def test_code_language_defaults_to_python_for_inline_and_unset_catalog_language():
    """ADR 0064: InlineCode has no language field at all (the builder's old free-text path only
    ever wrote Python); a CatalogCode with no language label defaults the same way."""
    from booth_pipeline.model import CatalogCode, InlineCode, code_language

    assert code_language(InlineCode(source="x = 1")) == "python"
    assert code_language(CatalogCode(entry_id="e", version="1")) == "python"
    assert code_language(CatalogCode(entry_id="e", version="1", language="sql")) == "sql"


def test_storage_reference_shape_is_just_backend_and_path():
    """ADR 0063: structurally parallel to CatalogCode, but no version to pin — the {backendId,
    path} pair IS the reference."""
    from booth_pipeline.model import code_language

    c = TaskConfig.model_validate({"code": {"type": "storage", "backendId": "b1", "path": "tasks/a.py"}})
    assert (c.code.backend_id, c.code.path) == ("b1", "tasks/a.py")
    assert not c.code.resolved  # nothing has been snapshotted yet
    assert code_language(c.code) == "python"  # unset language defaults the same way CatalogCode's does


def test_storage_code_is_resolved_once_source_and_sha256_are_both_present():
    from booth_pipeline.model import StorageCode

    assert not StorageCode(backend_id="b1", path="a.py").resolved
    assert not StorageCode(backend_id="b1", path="a.py", source="x").resolved  # sha256 still missing
    assert StorageCode(backend_id="b1", path="a.py", source="x", sha256="deadbeef").resolved


def test_an_old_pipeline_with_inline_code_still_loads_and_validates():
    """ADR 0063 removes the builder's path to CREATE new inline code, but does not delete the
    type: a task version saved before that ADR must keep loading and running exactly as it did."""
    c = TaskConfig.model_validate(task_config(source="def run(ctx):\n    return 1\n"))
    assert c.code.type == "inline"


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


# ---- Trigger union (ADR 0065, owned by Pipeline directly since ADR 0071) -----------------------


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
