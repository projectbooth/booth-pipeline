-- ADR 0124: an optional project on pipelines and tasks. A navigation label only: nothing that decides
-- who may do what, or when and as whom anything runs, reads it. No backfill: unassigned rows show
-- under "No project". Slugs are booth-core's (contracts/projects.md); this module only stores them.
ALTER TABLE pipelines ADD COLUMN project text;
ALTER TABLE tasks ADD COLUMN project text;
CREATE INDEX pipelines_workspace_project ON pipelines (workspace, project) WHERE project IS NOT NULL;
CREATE INDEX tasks_workspace_project ON tasks (workspace, project) WHERE project IS NOT NULL;
