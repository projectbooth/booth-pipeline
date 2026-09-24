"""``_pydantic_message`` — the handler that turns a raw pydantic v2 error into user-facing copy
(2026-09-23 pilot finding: a bare ``err["msg"]`` was reaching the frontend unmapped, e.g. "String
should have at least 1 character" the instant a task's code-source radio was switched before
anything was picked). Covered end to end, against real HTTP responses, in test_api.py; this is
just the mapping's own contract for every pydantic error ``type`` this codebase's models produce."""

from __future__ import annotations

from booth_pipeline.app import _pydantic_message
from booth_pipeline.model import PipelineSpec, TaskConfig


def errors_for(model: type, body: dict) -> list[dict]:
    try:
        model.model_validate(body)
    except Exception as e:  # noqa: BLE001 - pydantic's ValidationError, imported lazily by callers elsewhere too
        return e.errors()
    raise AssertionError("expected validation to fail")


def test_a_min_length_violation_of_one_reads_as_must_not_be_empty():
    (err,) = errors_for(TaskConfig, {"code": {"type": "catalog", "entryId": "", "version": "latest"}})
    assert _pydantic_message(err) == "must not be empty"


def test_a_missing_required_field_reads_as_is_required():
    (err,) = errors_for(TaskConfig, {"code": {"type": "catalog", "version": "latest"}})
    assert _pydantic_message(err) == "is required"


def test_an_unrecognized_discriminator_tag_names_what_is_expected():
    (err,) = errors_for(TaskConfig, {"code": {"type": "bogus"}})
    msg = _pydantic_message(err)
    assert "catalog" in msg and "storage" in msg and "inline" in msg
    assert "does not match any of the expected tags" not in msg  # not pydantic's own wording


def test_an_unknown_field_reads_as_not_a_recognized_field():
    (err,) = errors_for(TaskConfig, {"code": {"type": "inline", "source": "x=1"}, "bogusField": 1})
    assert _pydantic_message(err) == "is not a recognized field"


def test_an_upper_bound_violation_names_the_bound():
    (err,) = errors_for(TaskConfig, {"code": {"type": "inline", "source": "x=1"}, "retry": {"maxRetries": 99}})
    assert _pydantic_message(err) == "must be at most 10"


def test_a_lower_bound_violation_names_the_bound():
    (err,) = errors_for(TaskConfig, {"code": {"type": "inline", "source": "x=1"}, "timeoutSeconds": 0})
    assert _pydantic_message(err) == "must be at least 1"


def test_a_custom_validator_message_is_kept_as_written_not_remapped():
    """A `raise ValueError(...)` inside this codebase's own @field_validator is already
    user-facing — only pydantic's "Value error, " prefix is stripped, same as before."""
    (err,) = errors_for(PipelineSpec, {"tasks": [{"key": "Bad Key", "taskId": "t1", "taskVersion": 1}]})
    assert _pydantic_message(err) == "must be lowercase letters, digits and underscores, starting with a letter (max 63)"


def test_an_unmapped_error_type_never_leaks_pydantics_own_wording():
    assert _pydantic_message({"type": "some_future_pydantic_error_kind", "msg": "Pydantic's own internal wording"}) == "is invalid"


def test_a_type_mismatch_names_the_expected_type_generically():
    assert _pydantic_message({"type": "int_type", "msg": "Input should be a valid integer"}) == "must be a valid int"
