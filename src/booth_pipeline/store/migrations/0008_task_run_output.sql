-- What a task's successful attempt returned, kept for the run view (editors and owners only, like a Spark
-- driver log): {"value": <the output>} or, when it isn't kept, {"omitted": "tooLarge"|"unstorable", "bytes": n}.
-- NULL until an attempt succeeds, and for runs from before outputs were kept.
ALTER TABLE task_runs ADD COLUMN output jsonb;
