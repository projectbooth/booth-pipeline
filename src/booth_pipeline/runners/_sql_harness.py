"""Executed *inside* the base runner's subprocess for a SQL task (ADR 0064), never imported by
the service.

Unlike ``_harness.py`` this deliberately depends on ``duckdb`` (embedded, no external database to
install or reach) — a SQL task's entire job is to run a query, so a SQL engine is the one
dependency its execution cannot avoid. It runs under ``python -I`` exactly like the Python harness
(the subprocess uses the service's own interpreter, so a package on the service's own environment,
like duckdb, is importable — isolated mode only drops the *user* site directory and PYTHON*
env vars, not the interpreter's own site-packages); everything else here stays standard library.

Contract for task code (documented in the README):

* the task's source is exactly **one** SQL statement — typically a ``SELECT``, though anything
  DuckDB accepts runs (a ``CREATE TABLE ... AS`` or ``INSERT`` succeeds and reports its row count).
  Multiple statements are deliberately not supported here: splitting on ``;`` safely (respecting
  string literals, comments, dollar-quoting) is its own can of worms, and one statement per task
  keeps a SQL task's job the same shape as a Python task's.
* a direct upstream task's output is queryable as a table named after its task key, IF that
  output is a JSON list of objects (rows) — anything else upstream (a scalar, a single object,
  ``None``) is simply not registered as a table, and referencing it in SQL fails with DuckDB's own
  "table does not exist" error.
* ``params`` are available as DuckDB named parameters: ``$paramName``.
* the statement's result becomes the task's JSON output: a list of one JSON object per row (a DDL
  or DML statement with no rows of its own still reports DuckDB's own row-count column).
"""

import json
import os
import sys

MAX_OUTPUT_BYTES = 1024 * 1024


def _view_name(key: str) -> str:
    # Belt-and-braces, not the only thing standing between an upstream task key and SQL injection:
    # task keys are already restricted to TASK_KEY_RE (model.py) before a spec is ever saved.
    if not key or not all(c.isalnum() or c == "_" for c in key):
        raise ValueError(f"unsafe upstream task key {key!r}")
    return key


def main() -> int:
    workdir, result_path = sys.argv[1], sys.argv[2]
    with open(os.path.join(workdir, "invocation.json"), encoding="utf-8") as f:
        payload = json.load(f)
    with open(os.path.join(workdir, "task.sql"), encoding="utf-8") as f:
        sql = f.read()

    import duckdb  # imported after the payload/source are read: a missing dependency fails as this task, not before argv is even parsed

    con = duckdb.connect(":memory:")
    for key, value in payload["inputs"].items():
        if not isinstance(value, list):
            continue  # not tabular; simply not exposed as a table (see module docstring)
        name = _view_name(key)
        path = os.path.join(workdir, "in_" + name + ".json").replace("\\", "/")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(value, f)
        escaped = path.replace("'", "''")
        con.execute(f'CREATE VIEW "{name}" AS SELECT * FROM read_json_auto(\'{escaped}\')')

    try:
        cur = con.execute(sql, parameters=payload["params"] or {})
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
    except duckdb.Error as e:
        print(str(e), file=sys.stderr)
        return 1

    output = [dict(zip(cols, row, strict=True)) for row in rows]
    try:
        encoded = json.dumps({"output": output}, default=str)
    except (TypeError, ValueError) as e:
        print("task output is not JSON-serialisable: " + str(e), file=sys.stderr)
        return 3
    if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
        print(
            "task output exceeds " + str(MAX_OUTPUT_BYTES) + " bytes; pass large data through storage, not through ctx",
            file=sys.stderr,
        )
        return 3
    with open(result_path, "w", encoding="utf-8") as f:
        f.write(encoded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
