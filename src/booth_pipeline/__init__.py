"""booth-pipeline: Project Booth's pipeline module (nav group Build).

Pipeline (a DAG, drawn in the builder, versioned) -> Task (a node: source, transform or sink) ->
Job (a schedulable, triggerable instance of a pipeline). Backed by Dagster (ADR 0010); the base
runner needs no other module installed (ADR 0006).
"""

__version__ = "0.1.0"
