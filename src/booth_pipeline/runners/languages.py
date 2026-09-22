"""Per-language execution strategies for the base runner (ADR 0064).

``SubprocessRunner`` isolates a task by process regardless of language; what differs per language
is just which source filename it writes and which harness script it hands to the interpreter.
Adding a language means adding one entry here — nothing about the runner's subprocess isolation,
logging, cancellation or platform-token handling changes.

Scala/JAR execution is deliberately not here: it needs a JVM and its own submission story, which
booth-spark owns (ADR 0064) — the base runner stays "no other module needed" (the original brief),
and a language it can't run is simply not registered.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).parent


@dataclass(frozen=True)
class LanguageHandler:
    id: str
    source_filename: str
    harness: Path


class UnsupportedLanguage(Exception):
    """A task asked for a language the base runner has no execution strategy for."""


_HANDLERS: dict[str, LanguageHandler] = {
    h.id: h
    for h in (
        LanguageHandler(id="python", source_filename="task.py", harness=_HERE / "_harness.py"),
        LanguageHandler(id="sql", source_filename="task.sql", harness=_HERE / "_sql_harness.py"),
    )
}


def get(language: str) -> LanguageHandler:
    try:
        return _HANDLERS[language]
    except KeyError:
        raise UnsupportedLanguage(f"the base runner has no execution strategy for language {language!r}; available: {', '.join(available())}") from None


def available() -> tuple[str, ...]:
    return tuple(sorted(_HANDLERS))
