-- ADR 0073: plain "Save" writes a mutable draft, distinct from the immutable version history;
-- "Save as new version" stays exactly as it works today, and now also refreshes the draft (so the
-- draft is never behind the latest version). One nullable column per entity is enough — a NULL
-- draft_config/draft_spec means "nothing has ever been saved onto this task/pipeline", which a
-- brand-new, never-configured Task or empty Pipeline already starts as.

ALTER TABLE tasks ADD COLUMN draft_config jsonb;
ALTER TABLE tasks ADD COLUMN draft_updated_at timestamptz;
ALTER TABLE tasks ADD COLUMN draft_updated_by text;

ALTER TABLE pipelines ADD COLUMN draft_spec jsonb;
ALTER TABLE pipelines ADD COLUMN draft_updated_at timestamptz;
ALTER TABLE pipelines ADD COLUMN draft_updated_by text;
