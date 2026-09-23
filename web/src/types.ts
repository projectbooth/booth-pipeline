// Wire types for booth-pipeline's API (mirrors src/booth_pipeline/model.py and api.py). The
// terminology is fixed by ADR 0010 — Pipeline (the DAG), Task (a node), Job (a schedulable
// instance of a pipeline) — and is not to be renamed here.

export type WorkspaceRole = "owner" | "editor" | "viewer";

export type TaskKind = "source" | "transform" | "sink";
export type Backoff = "fixed" | "linear" | "exponential";

export interface RetryPolicy {
  maxRetries: number;
  delaySeconds: number;
  backoff: Backoff;
}

/** Code a Task runs. `catalog` is the v0 code-catalog reference shape (ADR 0010): just
 *  {entryId, version}. `storage` (ADR 0063) is structurally parallel — {backendId, path} — but
 *  has no version to pin, since a storage object has none. Both are filled in by the server at
 *  save time — a snapshot, so a run never depends on the catalog or storage being reachable.
 *  `inline` (free-text source typed into the builder) has no new creation path since ADR 0063,
 *  but old pipelines that already have it keep working. */
export type TaskCode =
  | { type: "inline"; source: string; sha256?: string | null }
  | {
      type: "catalog";
      entryId: string;
      version: string;
      name?: string | null;
      language?: string | null;
      sha256?: string | null;
      source?: string | null;
    }
  | {
      type: "storage";
      backendId: string;
      path: string;
      name?: string | null;
      language?: string | null;
      sha256?: string | null;
      source?: string | null;
    };

export interface Task {
  key: string;
  name: string;
  /** Purely decorative (ADR 0062): a label for the canvas, never a constraint on dependency
   *  wiring. A task doesn't have to be tagged at all. */
  kind: TaskKind | null;
  code: TaskCode;
  runner: string;
  retry: RetryPolicy | null;
  dependsOn: string[];
  params: Record<string, unknown>;
  timeoutSeconds: number;
  /** Opt in to a short-lived platform token so the task can use ctx.storage / ctx.catalog (ADR 0056). */
  platformAccess: boolean;
  position: { x: number; y: number };
}

export interface PipelineSpec {
  tasks: Task[];
}

export interface Pipeline {
  id: string;
  name: string;
  description: string;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
  latestVersion: number;
}

export interface PipelineVersionSummary {
  pipelineId: string;
  version: number;
  notes: string;
  createdBy: string;
  createdAt: string;
  taskCount: number;
}

export interface PipelineVersion extends PipelineVersionSummary {
  spec: PipelineSpec;
}

/** A basic cron schedule (5 fields, minute granularity) in an IANA timezone — the original
 *  mechanism, unchanged (ADR 0065). */
export interface CronTrigger {
  type: "cron";
  cron: string;
  timezone: string;
  enabled: boolean;
}

/** Run every N seconds — for schedules below cron's one-minute floor (ADR 0065). */
export interface IntervalTrigger {
  type: "interval";
  seconds: number;
  enabled: boolean;
}

/** A job's schedule (ADR 0065): a discriminated union so a future trigger type is a new member,
 *  not a redesign. `Schedule` is kept as an alias for the pre-0065 name; prefer `Trigger`. */
export type Trigger = CronTrigger | IntervalTrigger;
export type Schedule = CronTrigger;

export interface Job {
  id: string;
  name: string;
  pipelineId: string;
  pipelineVersion: number | null;
  schedule: Trigger | null;
  retry: RetryPolicy | null;
  allowConcurrentRuns: boolean;
  /** The most a run's platform token may carry, whatever its owner holds. Never "owner". */
  roleCeiling: "viewer" | "editor";
  /** False for a job created before workload identity: its unattended runs cannot get platform access until it is saved again. */
  hasOwner: boolean;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
  nextRunAt: string | null;
}

export interface JobInput {
  name: string;
  pipelineId: string;
  pipelineVersion: number | null;
  schedule: Trigger | null;
  retry: RetryPolicy | null;
  allowConcurrentRuns: boolean;
  roleCeiling: "viewer" | "editor";
}

export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "canceled";
export type TaskStatus = "pending" | "running" | "retrying" | "succeeded" | "failed" | "skipped" | "canceled";

export interface Run {
  id: string;
  jobId: string;
  pipelineId: string;
  pipelineVersion: number;
  status: RunStatus;
  trigger: "manual" | "schedule";
  triggeredBy: string;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  error: string | null;
  cancelRequested: boolean;
}

export interface TaskRun {
  taskKey: string;
  status: TaskStatus;
  attempts: number;
  startedAt: string | null;
  finishedAt: string | null;
  error: string | null;
}

export interface RunDetail extends Run {
  tasks: TaskRun[];
}

export interface LogLine {
  seq: number;
  ts: string;
  taskKey: string | null;
  attempt: number;
  stream: "stdout" | "stderr" | "system";
  message: string;
}

export interface LogPage {
  items: LogLine[];
  done: boolean;
}

export interface Page<T> {
  items: T[];
  total: number;
}

export interface RunnerInfo {
  id: string;
  displayName: string;
  available: boolean;
  reason: string | null;
}

export interface ValidateResult {
  valid: boolean;
  error?: string;
  field?: string | null;
}

// booth-catalog's code catalog, as much of it as the picker needs (its own API; read-only here).
export interface CatalogCodeEntry {
  id: string;
  name: string;
  description: string;
  language: string;
  latestVersion: { version: string } | null;
  versionCount: number;
}

export interface CatalogVersionSummary {
  version: string;
  seq: number;
  notes: string;
}

// booth-storage, as much of it as the picker needs (its own API; read-only here, ADR 0063).
export interface StorageBackendSummary {
  id: string;
  displayName: string;
}

export interface StorageObjectSummary {
  path: string;
  size?: number;
}
