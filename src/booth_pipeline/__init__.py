"""booth-pipeline: Project Booth's pipeline module (nav group Build).

Task (a standalone, independently versioned, reusable unit of code) -> Pipeline (a DAG of task
references, drawn in the builder, versioned, and the thing that owns its own scheduling, triggering
and run history directly — ADR 0071 retired the separate Job entity). Backed by Dagster (ADR 0010);
the base runner needs no other module installed (ADR 0006).
"""

__version__ = "0.1.0"
