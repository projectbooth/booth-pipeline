-- ADR 0128 and its Amendment 1: a workspace owner's task secrets. Sealed with AES-256-GCM (workspace and
-- name as associated data); the key is never here, only in the operator's Kubernetes Secret, so this
-- table alone opens nothing. key_id names the key that sealed a row, so the key can be rotated.
CREATE TABLE task_secrets (
    workspace text NOT NULL,
    name text NOT NULL,
    ciphertext bytea NOT NULL,
    nonce bytea NOT NULL,
    key_id text NOT NULL,
    updated_by text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (workspace, name)
);
CREATE INDEX task_secrets_key_id ON task_secrets (key_id);
-- The secret NAMES a task's latest attempt was given (never values), for the run page.
ALTER TABLE task_runs ADD COLUMN secrets jsonb;
