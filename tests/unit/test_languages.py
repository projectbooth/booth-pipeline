"""The base runner's per-language dispatch (ADR 0064): a language string maps to how its source
file is named and which harness script runs it. Execution itself is covered end to end in
test_engine.py (a real SQL task) and test_api.py (the save-time language gate); this is just the
registry's own contract."""

from __future__ import annotations

import pytest

from booth_pipeline.runners import languages


def test_python_and_sql_are_both_registered_with_distinct_source_files_and_harnesses():
    py, sql = languages.get("python"), languages.get("sql")
    assert py.source_filename == "task.py" and sql.source_filename == "task.sql"
    assert py.harness != sql.harness
    assert py.harness.exists() and sql.harness.exists()


def test_available_lists_every_registered_language_sorted():
    assert languages.available() == ("python", "sql")


def test_an_unregistered_language_raises_naming_what_is_available():
    with pytest.raises(languages.UnsupportedLanguage, match="python.*sql|sql.*python"):
        languages.get("scala")
