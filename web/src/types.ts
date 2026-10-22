// Wire types for booth-pipeline's API (mirrors src/booth_pipeline/model.py and api.py). The
// terminology is fixed by ADR 0010, restructured by ADR 0071 — Task (a standalone, independently
// versioned, reusable unit of code) and Pipeline (the DAG, whose nodes reference a task's specific
// version) are the whole model; Job is retired, and Pipeline owns scheduling/triggering/run
// history directly. Not to be renamed here.

export type WorkspaceRole = "owner" | "editor" | "viewer";

export type Backoff = "fixed" | "linear" | "exponential";

export interface RetryPolicy {
  maxRetries: number;
  delaySeconds: number;
  backoff: Backoff;
}

/** Code a Task version runs. `catalog` is the v0 code-catalog reference shape (ADR 0010): just
 *  {entryId, version}. `storage` (ADR 0063) is structurally parallel — {backendId, path} — but
 *  has no version to pin, since a storage object has none. Both are filled in by the server at
 *  save time — a snapshot, so a run never depends on the catalog or storage being reachable.
 *  `inline` (free-text source typed into the builder) has no new creation path since ADR 0063,
 *  but old tasks that already have it keep working. */
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

/** One version's worth of a standalone Task's configuration (ADR 0071) — what actually runs. No
 *  `key`, `dependsOn` or `position`: those describe how a *reference* to this task is wired into
 *  one particular pipeline's DAG, not anything about the task itself. No `kind` either — ADR 0062
 *  made it decorative, ADR 0071 removes it outright. */
export interface TaskConfig {
  code: TaskCode;
  runner: string;
  retry: RetryPolicy | null;
  params: Record<string, unknown>;
  timeoutSeconds: number;
  /** Opt in to a short-lived platform token so the task can use ctx.storage / ctx.catalog (ADR 0056). */
  platformAccess: boolean;
  /** Python packages installed into the task's own pod before it runs (ADR 0115). Absent from a
   *  backend older than 0.1.5. */
  requirements?: string[];
  /** ADR 0119: what a Spark task (runner "spark") asks booth-spark for. Absent before 0.2.0. */
  spark?: SparkOptions | null;
}

export interface SparkStorageLocation {
  backendId: string;
  path: string;
  access?: "read" | "readwrite";
}

export interface SparkOptions {
  resources?: { driver?: { memory?: string }; executors?: { min?: number; max?: number; memory?: string } };
  conf?: Record<string, string>;
  args?: string[];
  dataAccess?: { database?: boolean; lakehouse?: boolean; storage?: SparkStorageLocation[] };
}

/** A standalone, versioned, reusable Task (ADR 0071) — the direct counterpart of Pipeline. */
export interface TaskEntity {
  id: string;
  name: string;
  description: string;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
  latestVersion: number;
  /** Whether a mutable draft (ADR 0073) has ever been saved onto this task — separate from
   *  latestVersion: a task can be "configured" via its draft alone, never promoted to a version. */
  hasDraft: boolean;
  draftUpdatedAt: string | null;
}

/** A task's mutable, unversioned "current" state (ADR 0073) — what plain "Save" writes to,
 *  distinct from the immutable version list. "Always follow latest" resolves here first. */
export interface TaskDraft {
  taskId: string;
  config: TaskConfig;
  updatedBy: string;
  updatedAt: string;
}

export interface TaskVersionSummary {
  taskId: string;
  version: number;
  notes: string;
  createdBy: string;
  createdAt: string;
}

export interface TaskVersion extends TaskVersionSummary {
  config: TaskConfig;
}

/** One node in a Pipeline's DAG (ADR 0071): a reference to a specific standalone Task's
 *  (taskId, taskVersion) pair, plus this pipeline's own wiring around it. `taskVersion` is either
 *  a concrete saved version number or "latest" (resolved to a concrete version by the server at
 *  save time — never a floating pointer once stored). */
export interface TaskRef {
  key: string;
  taskId: string;
  taskVersion: number | "latest";
  dependsOn: string[];
  position: { x: number; y: number };
  /** ADR 0078: this node's own values for the task's params, merged over the task's saved params
   *  (shallow, key by key) when it runs. Saved with the pipeline version. Absent on specs saved
   *  before ADR 0078, which the server reads as {}. */
  paramOverrides?: Record<string, unknown>;
}

export interface PipelineSpec {
  tasks: TaskRef[];
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

/** A pipeline's schedule (ADR 0065): a discriminated union so a future trigger type is a new
 *  member, not a redesign. `Schedule` is kept as an alias for the pre-0065 name; prefer `Trigger`. */
export type Trigger = CronTrigger | IntervalTrigger;
export type Schedule = CronTrigger;

export interface Pipeline {
  id: string;
  name: string;
  description: string;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
  latestVersion: number;
  // Scheduling/triggering/ownership, owned directly by Pipeline (ADR 0071 retires the separate
  // Job entity these used to live on).
  schedule: Trigger | null;
  allowConcurrentRuns: boolean;
  /** The most a run's platform token may carry, whatever its owner holds. Never "owner". */
  roleCeiling: "viewer" | "editor";
  /** False for a pipeline scheduled before workload identity: its unattended runs cannot get
   *  platform access until its schedule is saved again. */
  hasOwner: boolean;
  /** Who its scheduled runs act as (ADR 0120). Absent from a backend older than 0.2.0. */
  owner?: OwnerInfo | null;
  nextRunAt: string | null;
  /** null: every run tracks the latest saved version. Set: every run targets exactly this
   *  version, regardless of what is saved on top of it later. */
  pinnedVersion: number | null;
  /** Whether a mutable draft (ADR 0073) has ever been saved onto this pipeline. */
  hasDraft: boolean;
  draftUpdatedAt: string | null;
}

/** A pipeline's mutable, unversioned "current" state (ADR 0073) — the direct counterpart of
 *  TaskDraft. What plain "Save" writes to, distinct from the immutable version list. */
export interface PipelineDraft {
  pipelineId: string;
  spec: PipelineSpec;
  updatedBy: string;
  updatedAt: string;
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

export interface PipelineScheduleUpdate {
  schedule: Trigger | null;
  allowConcurrentRuns: boolean;
  roleCeiling: "viewer" | "editor";
  pinnedVersion: number | null;
}

export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "canceled";
export type TaskStatus = "pending" | "running" | "retrying" | "succeeded" | "failed" | "skipped" | "canceled";

export interface Run {
  id: string;
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
  /** Who the run acted as: the pipeline's owner (scheduled) or who pressed Run now. */
  actsAs?: OwnerInfo | null;
}

/** A pipeline's owner or a run's identity (ADR 0120). A service account's id is shown; a person's
 *  `sub` never is (`you` says whether it is the caller). */
export interface OwnerInfo {
  kind: "person" | "service";
  id?: string;
  name: string;
  you: boolean;
}

/** booth-core's GET /api/service-accounts (workspace owners only; ADR 0120). */
export interface CoreServiceAccount {
  id: string;
  name: string;
  role: "viewer" | "editor";
  description: string;
  disabled: boolean;
  createdBy: string;
  createdAt: string;
}

/** booth-core's GET /api/service-accounts/{name} (any member): enough for the "Owner disabled" badge. */
export interface CoreServiceAccountState {
  name: string;
  role: "viewer" | "editor";
  disabled: boolean;
}

/** GET /service-accounts/uses: which pipelines run as each service account (core keeps no record). */
export interface ServiceAccountUse {
  account: string;
  pipelines: { id: string; name: string; scheduled: boolean }[];
}

export interface TaskRun {
  taskKey: string;
  status: TaskStatus;
  attempts: number;
  startedAt: string | null;
  finishedAt: string | null;
  error: string | null;
  /** ADR 0119: where the latest attempt ran outside its pod (a booth-spark application). */
  external?: { kind: "spark"; id: string; state: string; reason: string | null } | null;
  /** What the successful attempt returned. Editors and owners only (absent for viewers); null until an
   *  attempt succeeds, and for runs from before outputs were kept. */
  output?: TaskOutput | null;
}

/** A task's output as the run keeps it: the value, or why it wasn't kept (it still reached its downstream tasks). */
export type TaskOutput = { value: unknown } | { omitted: "tooLarge" | "unstorable"; bytes: number };

export interface RunDetail extends Run {
  tasks: TaskRun[];
}

export interface LogLine {
  seq: number;
  ts: string;
  taskKey: string | null;
  attempt: number;
  /** "spark": a Spark task's driver log (shown to editors and owners only). */
  stream: "stdout" | "stderr" | "system" | "spark";
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

/** Whether this install can install a task's Python packages (ADR 0115), from GET /api/config. */
export interface PipStatus {
  available: boolean;
  /** Why not, naming the value an operator sets. */
  reason?: string;
  /** How task pods reach the index: index, internet, extra or unrestricted. */
  route?: string;
}

/** GET /api/config: the module's limits and capabilities. */
export interface ModuleConfig {
  limits: Record<string, number>;
  catalogConfigured: boolean;
  /** Absent from a backend older than 0.1.5. */
  pip?: PipStatus;
  /** ADR 0119: the Spark runner's settings, when this install has it. */
  spark?: { retryMinDelaySeconds: number } | null;
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
