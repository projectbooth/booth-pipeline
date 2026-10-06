"""Every field of a task invocation reaches the runner service, or is declared local on purpose.

ADR 0064 added ``TaskInvocation.language`` but never put it on the runner service's wire protocol,
so every SQL task ran as Python in every deployment for two weeks (fixed 2026-10-06). Nothing failed:
the far side quietly used the default. This test enumerates the fields from the dataclasses
themselves, gives each a value its default can't be mistaken for, sends the invocation through the
real RemoteRunner over HTTP to the real runner service, and compares what arrived. A field added
later fails here until it is wired through (RemoteRunner's body, RunBody, the service's
TaskInvocation) or listed in LOCAL with a one-line reason.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import types
import typing
from typing import Any

from booth_pipeline.runner_service import create_runner_app
from booth_pipeline.runners.base import Cancellation, TaskAccess, TaskInvocation
from booth_pipeline.runners.remote import RemoteRunner

from .servers import serve_asgi

# Fields that deliberately never leave the API/scheduler pod. Each needs a reason.
LOCAL = {
    "TaskAccess.refresh": "a callable that mints with the module's credential; it stays in the API pod, which PUTs fresh tokens instead",
    "TaskAccess.role": "only the API pod uses it, to choose each credential sidecar's --access; the task's token already carries the role",
}


def sample(cls: type, prefix: str) -> Any:
    """An instance whose every field holds a distinctive value, built from the field's own type."""
    hints = typing.get_type_hints(cls)
    values = {}
    for i, f in enumerate(dataclasses.fields(cls)):
        values[f.name] = _value(hints[f.name], f"{prefix}{f.name}", 100 + i, f"{cls.__name__}.{f.name}")
    return cls(**values)


def _value(tp: Any, name: str, n: int, where: str) -> Any:
    if typing.get_origin(tp) in (typing.Union, types.UnionType):  # X | None: the non-None side
        (tp,) = [a for a in typing.get_args(tp) if a is not type(None)]
    origin = typing.get_origin(tp) or tp
    if tp is str:
        return f"sample-{name}"
    if tp is int:
        return n
    if origin is dict:
        return {name: [n, "nested"]}
    if dataclasses.is_dataclass(tp):
        return sample(tp, f"{name}.")
    if origin is collections.abc.Callable:
        return lambda: f"refreshed-{name}"
    raise AssertionError(f"{where}: no sample value for type {tp!r}; teach _value() this type, then wire the field or mark it LOCAL")


def compare(cls: type, sent: Any, got: Any, path: str = "") -> list[str]:
    """Every field that didn't survive the trip and isn't declared LOCAL."""
    lost = []
    for f in dataclasses.fields(cls):
        key = f"{cls.__name__}.{f.name}"
        a, b = getattr(sent, f.name), getattr(got, f.name)
        if key in LOCAL:
            continue
        if dataclasses.is_dataclass(a) and b is not None:
            lost += compare(type(a), a, b, f"{path}{f.name}.")
        elif a != b:
            lost.append(f"{path}{f.name}: sent {a!r}, runner service got {b!r}")
    return lost


class Capture:
    """Stands in for the runner service's engine: records the invocation it was handed."""

    def __init__(self) -> None:
        self.got: TaskInvocation | None = None

    def run(self, inv: TaskInvocation, log, cancel) -> None:
        self.got = inv

    def update_token(self, run_id: str, task_key: str, token: str) -> bool:
        return False


class Lines:
    def line(self, stream: str, message: str) -> None:
        pass


def test_every_task_invocation_field_reaches_the_runner_service():
    sent = sample(TaskInvocation, "")
    capture = Capture()
    with serve_asgi(create_runner_app("s", capture)) as url:
        RemoteRunner(url, "s").run(sent, Lines(), Cancellation())
    assert capture.got is not None, "the runner service never ran the task"
    lost = compare(TaskInvocation, sent, capture.got)
    assert not lost, "fields dropped on the way to the runner service (wire them through, or mark them LOCAL with a reason):\n" + "\n".join(lost)


def test_local_fields_exist_and_really_stay_local():
    """LOCAL can't hide a field that no longer exists, or one that in fact crosses the wire."""
    names = {f"{c.__name__}.{f.name}" for c in (TaskInvocation, TaskAccess) for f in dataclasses.fields(c)}
    assert set(LOCAL) <= names, f"LOCAL names fields that don't exist: {set(LOCAL) - names}"
    sent = sample(TaskInvocation, "")
    capture = Capture()
    with serve_asgi(create_runner_app("s", capture)) as url:
        RemoteRunner(url, "s").run(sent, Lines(), Cancellation())
    for key in LOCAL:
        cls_name, field = key.split(".")
        a = sent.access if cls_name == "TaskAccess" else sent
        b = capture.got.access if cls_name == "TaskAccess" else capture.got
        assert getattr(a, field) != getattr(b, field), f"{key} is marked LOCAL but arrives intact: drop it from LOCAL"


def test_the_sample_values_are_never_the_defaults():
    """Otherwise a field the far side silently defaults (as language did) would pass."""
    for cls in (TaskInvocation, TaskAccess):
        inst = sample(cls, "")
        for f in dataclasses.fields(cls):
            if f.default is not dataclasses.MISSING:
                assert getattr(inst, f.name) != f.default, f"{cls.__name__}.{f.name} sample equals its default"
