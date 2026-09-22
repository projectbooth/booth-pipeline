-- Workload identity (ADR 0056/0058). A run that opts in to platform access needs to name whose live
-- role caps its token (`owner_sub`, a user's `sub` — never a display name) and how much it may be
-- granted at most (`role_ceiling`). Existing rows get an empty owner: their runs simply cannot mint
-- (core refuses an unknown owner), which is the safe default for jobs created before this existed.
ALTER TABLE jobs ADD COLUMN owner_sub text NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN role_ceiling text NOT NULL DEFAULT 'editor';
ALTER TABLE runs ADD COLUMN owner_sub text NOT NULL DEFAULT '';
