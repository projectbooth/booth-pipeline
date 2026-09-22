# 0010: Adopting ADR 0064 — a SQL runner + a real language-dispatch registry

Status: **built.**

## What changed

- `runners/languages.py` (new): the base runner's per-language dispatch, as a real registry —
  `LanguageHandler(id, source_filename, harness)` — instead of the hardcoded Python-only check
  `service.py` used to have. `get(language)` returns a handler or raises `UnsupportedLanguage`
  naming what is available; `available()` lists every registered id. Two are registered:
  `python` (unchanged: `task.py` + the existing `_harness.py`) and `sql` (new: `task.sql` + a new
  `_sql_harness.py`). Adding a language later is one entry here — nothing about
  `SubprocessRunner`'s process isolation, logging, timeout or cancellation changes per language.
- `runners/_sql_harness.py` (new): executed inside the base runner's subprocess for a SQL task,
  same as `_harness.py` is for Python. Runs on DuckDB (embedded, no server to install or reach) —
  the one dependency this harness cannot avoid, everything else in it is standard library, same
  discipline as the Python harness. Contract: a task's source is **exactly one** SQL statement
  (splitting multiple statements safely — string literals, comments, dollar-quoting — was ruled
  out as unnecessary complexity for v0); a direct upstream task's output becomes a queryable table
  named after its task key **only if** that output is a JSON list of objects (rows) — anything
  else upstream is simply not registered as a table, and referencing it fails with DuckDB's own
  "table does not exist"; `params` are available as DuckDB named parameters (`$paramName`); the
  statement's result rows become the task's JSON output, exactly like a Python task's return
  value, so a downstream task (Python or SQL) consumes it identically either way.
- `runners/base.py`: `TaskInvocation` gained `language: str = "python"` — what
  `SubprocessRunner._run_in` passes to `languages.get(...)` to decide which file to write the
  source to and which harness to hand to the interpreter.
- `model.py`: added `code_language(code) -> str`. `InlineCode` has no `language` field at all — it
  is the builder's old free-text path (ADR 0063 removes its UI, but the type itself is unchanged
  and back-compat), which only ever produced Python — so it is always `"python"`. `CatalogCode`'s
  existing `language` field (an advisory label from the catalog) is used when set, defaulting the
  same way when it is not.
- `engine.py`: `_make_op`'s `compute()` now passes `language=code_language(task.code)` into every
  `TaskInvocation` it builds.
- `service.py`: `_resolve_code`'s save-time gate — previously
  `code.language not in ("", "python")` raising a fixed "the base runner runs Python" message —
  now checks `code_language(code) not in languages.available()`, with the error naming whatever is
  actually registered. A SQL catalog reference now saves and runs; anything neither runner knows
  about (Scala, say) is still refused at save time, just with an accurate reason.
- `pyproject.toml`: added `duckdb>=1.0` as a core dependency. This does not weaken "the base
  runner needs zero other **modules**" (the original brief's requirement, about other Booth
  modules — catalog, storage — not needing to be installed): DuckDB is an embedded library, not a
  service to reach, same category as `dagster` or `psycopg` already being dependencies.
- Scala/JAR execution was deliberately **not** added anywhere — it needs a JVM and its own
  submission story, which is booth-spark's to own per the ADR, not something bolted onto the base
  runner.

## Verified

- DuckDB's Python API shape (named-parameter binding via a dict, `cursor.description` /
  `.fetchall()`, registering a JSON file as a view via `read_json_auto`, exception hierarchy for
  `duckdb.Error`) was checked empirically against the installed version (1.5.5) with a throwaway
  script before writing the harness, rather than assumed — including the edge cases an empty
  upstream list, a `None`-valued column, and a missing named parameter all behave predictably (the
  last raises `duckdb.InvalidInputException`, a `duckdb.Error` subclass, so the harness's own
  `except duckdb.Error` catches it).
- Confirmed `python -I` (the harness's isolated-mode invocation) still sees the invoking
  interpreter's own site-packages — only the *user* site directory and `PYTHON*` env vars are
  dropped — so `duckdb`, installed alongside the service's own dependencies, is importable inside
  the subprocess without changing how the harness is invoked.
- `test_engine.py`: a real end-to-end SQL task (`SELECT sum(n) AS total FROM extract`) queries a
  direct upstream's tabular output and a downstream Python task sees the resulting row exactly
  like any other upstream output (asserted via its printed log line, the same pattern the existing
  Python-to-Python test already uses); a separate test confirms a non-tabular upstream output
  (a bare scalar) is simply not exposed as a table, and the task fails with DuckDB's own error
  rather than crashing the run.
- `test_api.py`: a SQL catalog entry now saves and **runs to completion** through the real HTTP
  API and the real subprocess path (replacing the old test that asserted SQL was refused); a
  language neither runner supports (`scala`) is still refused at save time, with the error naming
  both `python` and `sql` as what is actually available.
- `test_model.py`: `code_language` defaults to `"python"` for `InlineCode` and for a `CatalogCode`
  with no language label, and reports the label when one is set.
- `test_languages.py` (new): both languages are registered with distinct source filenames and
  harness files that actually exist on disk; `available()` is sorted; an unregistered language
  raises naming what is available.
- Full suite green: 280 Python (`pytest tests/unit tests/contract`, real Postgres via
  `hack/docker-compose.test.yml`), `ruff check` clean.

## Not done

- A SQL task has no `ctx.storage` / `ctx.catalog` equivalent — platform access (ADR 0056) is a
  Python `ctx` object concept, and the SQL harness doesn't wire up the platform token at all. A
  SQL task's only inputs are its direct upstreams' tabular outputs and its own `params`; nothing
  in the ADR asked for more than that, and it keeps the harness's contract simple to document.
- No multi-statement support (`;`-separated SQL, `CREATE VIEW` followed by a `SELECT`, etc.) — see
  "What changed" above for why. If a later brief needs it, the harness is the one place to extend,
  same shape as adding a new language is now.
- No new UI surfaced for authoring SQL directly — per ADR 0063, code authoring is moving to
  catalog/storage references only, not staying in the builder, so a SQL task's source comes from
  wherever ADR 0063's picker resolves it from, not a new "write code here" mode for this ADR to
  add.
