import type {
  CatalogCodeEntry,
  CatalogVersionSummary,
  EventTriggerStatus,
  LogPage,
  Page,
  Pipeline,
  PipelineDraft,
  Project,
  PipelineScheduleUpdate,
  PipelineSpec,
  PipelineVersion,
  PipelineVersionSummary,
  Run,
  RunDetail,
  ModuleConfig,
  RunnerInfo,
  ServiceAccountUse,
  CoreServiceAccount,
  CoreServiceAccountState,
  RunStatus,
  StorageBackendSummary,
  StorageObjectSummary,
  TaskConfig,
  TaskDraft,
  TaskEntity,
  TaskVersion,
  TaskVersionSummary,
  ValidateResult,
} from "../types";

// This component is mounted by booth-design's shell, so its requests resolve against the shell's
// origin and must go through booth-core's gateway at /modules/{id}/* — which strips the
// /modules/pipeline prefix before forwarding to this module's own routes (/api/...). A bare
// "/api/..." would hit booth-core's own API instead. The dev harness's Vite proxy mimics the same
// prefix-stripping so identical paths work standalone.
const PIPELINE = "/modules/pipeline/api";
// booth-catalog is called through the same gateway, with the user's own token, only for the code
// picker. The pipeline BACKEND resolves the chosen entry itself at save time; this is browse-only.
const CATALOG = "/modules/catalog/api";
// booth-storage, likewise browse-only (ADR 0063): the pipeline backend resolves and snapshots the
// chosen {backendId, path} itself at save time.
const STORAGE = "/modules/storage/api";
const SPARK = "/modules/spark";
const CORE = ""; // booth-core's own API, on the shell's origin (/api/...)

export type GetAccessToken = () => string | null;

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    /** The request field a validation error is about ("tasks[2].dependsOn"), when the server named one. */
    public field?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Everything a request needs from the mounting shell (ADR 0031/0033). */
export interface ApiContext {
  workspace: string;
  getAccessToken: GetAccessToken;
}

// workspace sets the X-Workspace header booth-core's gateway requires on every authenticated
// request (ADR 0025). getAccessToken is called fresh immediately before each request, never
// cached — the shell's token can be silently renewed at any time (ADR 0032/0033). A null return
// omits the Authorization header rather than sending the literal string "null".
function buildHeaders(ctx: ApiContext, init?: RequestInit): Headers {
  const headers = new Headers(init?.headers);
  headers.set("X-Workspace", ctx.workspace);
  const token = ctx.getAccessToken();
  if (token !== null) headers.set("Authorization", `Bearer ${token}`);
  return headers;
}

async function toApiError(res: Response): Promise<ApiError> {
  const text = await res.text();
  try {
    const body = JSON.parse(text) as { error?: string; field?: string };
    if (body.error) return new ApiError(res.status, body.error, body.field);
  } catch {
    // not JSON — e.g. a gateway error page; fall through to the raw text
  }
  return new ApiError(res.status, text || res.statusText || `HTTP ${res.status}`);
}

async function request<T>(ctx: ApiContext, base: string, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(base + path, { ...init, headers: buildHeaders(ctx, init) });
  if (!res.ok) throw await toApiError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// Like `request`, but for an endpoint whose body is not JSON (the YAML export).
async function requestText(ctx: ApiContext, base: string, path: string, init?: RequestInit): Promise<string> {
  const res = await fetch(base + path, { ...init, headers: buildHeaders(ctx, init) });
  if (!res.ok) throw await toApiError(res);
  return res.text();
}

function json(method: string, body?: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) };
}

function qs(params: Record<string, string | number | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

const e = encodeURIComponent;

export const api = {
  runners: (c: ApiContext) => request<{ items: RunnerInfo[] }>(c, PIPELINE, "/runners").then((r) => r.items),
  config: (c: ApiContext) => request<ModuleConfig>(c, PIPELINE, "/config"),
  setOwner: (c: ApiContext, id: string, owner: string) => request<Pipeline>(c, PIPELINE, `/pipelines/${e(id)}/owner`, json("PUT", { owner })),
  /** booth-spark's limits for this caller (ADR 0119), for the Spark panel's hints. */
  sparkLimits: (c: ApiContext) => request<Record<string, unknown>>(c, SPARK, "/v1/limits"),
  /** booth-core: the workspace's service accounts (workspace owners only; ADR 0120). */
  serviceAccounts: (c: ApiContext) => request<CoreServiceAccount[]>(c, CORE, "/api/service-accounts"),
  /** booth-core: one account's role and whether it is disabled (any member). */
  serviceAccount: (c: ApiContext, name: string) => request<CoreServiceAccountState>(c, CORE, `/api/service-accounts/${e(name)}`),
  serviceAccountUses: (c: ApiContext) => request<{ items: ServiceAccountUse[] }>(c, PIPELINE, "/service-accounts/uses").then((r) => r.items),

  listPipelines: (c: ApiContext, q = "", project?: string | null) => request<Page<Pipeline>>(c, PIPELINE, `/pipelines${qs({ q, project: project ?? undefined, limit: 200 })}`),
  getPipeline: (c: ApiContext, id: string) => request<Pipeline>(c, PIPELINE, `/pipelines/${e(id)}`),
  // `project`: undefined leaves it out of the body (kept as it is, or none); null clears it (ADR 0124).
  createPipeline: (c: ApiContext, name: string, description: string, project?: string | null) =>
    request<Pipeline>(c, PIPELINE, "/pipelines", json("POST", { name, description, ...(project !== undefined ? { project } : {}) })),
  updatePipeline: (c: ApiContext, id: string, name: string, description: string, project?: string | null) =>
    request<Pipeline>(c, PIPELINE, `/pipelines/${e(id)}`, json("PUT", { name, description, ...(project !== undefined ? { project } : {}) })),
  deletePipeline: (c: ApiContext, id: string) => request<void>(c, PIPELINE, `/pipelines/${e(id)}`, { method: "DELETE" }),
  listVersions: (c: ApiContext, id: string) => request<Page<PipelineVersionSummary>>(c, PIPELINE, `/pipelines/${e(id)}/versions?limit=200`),
  getVersion: (c: ApiContext, id: string, version: number | "latest") =>
    request<PipelineVersion>(c, PIPELINE, `/pipelines/${e(id)}/versions/${version}`),
  saveVersion: (c: ApiContext, id: string, spec: PipelineSpec, notes = "") =>
    request<PipelineVersion>(c, PIPELINE, `/pipelines/${e(id)}/versions`, json("POST", { spec, notes })),
  getPipelineDraft: (c: ApiContext, id: string) => request<PipelineDraft>(c, PIPELINE, `/pipelines/${e(id)}/draft`),
  savePipelineDraft: (c: ApiContext, id: string, spec: PipelineSpec) =>
    request<PipelineDraft>(c, PIPELINE, `/pipelines/${e(id)}/draft`, json("PUT", { spec })),
  validate: (c: ApiContext, spec: PipelineSpec) => request<ValidateResult>(c, PIPELINE, "/pipelines/validate", json("POST", { spec })),
  exportVersion: (c: ApiContext, id: string, version: number | "latest") =>
    requestText(c, PIPELINE, `/pipelines/${e(id)}/versions/${version}/export`),
  updateSchedule: (c: ApiContext, id: string, body: PipelineScheduleUpdate) =>
    request<Pipeline>(c, PIPELINE, `/pipelines/${e(id)}/schedule`, json("PUT", body)),
  eventTriggerStatus: (c: ApiContext, id: string) => request<EventTriggerStatus>(c, PIPELINE, `/pipelines/${e(id)}/event-trigger`),
  runPipeline: (c: ApiContext, id: string) => request<Run>(c, PIPELINE, `/pipelines/${e(id)}/run`, { method: "POST" }),

  listTasks: (c: ApiContext, q = "", project?: string | null) => request<Page<TaskEntity>>(c, PIPELINE, `/tasks${qs({ q, project: project ?? undefined, limit: 200 })}`),
  getTask: (c: ApiContext, id: string) => request<TaskEntity>(c, PIPELINE, `/tasks/${e(id)}`),
  createTask: (c: ApiContext, name: string, description: string, config?: TaskConfig, project?: string | null) =>
    request<TaskEntity & { version: TaskVersion | null }>(c, PIPELINE, "/tasks", json("POST", { name, description, config, ...(project !== undefined ? { project } : {}) })),
  updateTask: (c: ApiContext, id: string, name: string, description: string, project?: string | null) =>
    request<TaskEntity>(c, PIPELINE, `/tasks/${e(id)}`, json("PUT", { name, description, ...(project !== undefined ? { project } : {}) })),
  /** booth-core's project registry (ADR 0124), read as the user. An array, or an `items` page. */
  projects: (c: ApiContext) => request<Project[] | { items: Project[] }>(c, CORE, "/api/projects").then((r) => (Array.isArray(r) ? r : r.items)),
  deleteTask: (c: ApiContext, id: string) => request<void>(c, PIPELINE, `/tasks/${e(id)}`, { method: "DELETE" }),
  listTaskVersions: (c: ApiContext, id: string) => request<Page<TaskVersionSummary>>(c, PIPELINE, `/tasks/${e(id)}/versions?limit=200`),
  getTaskVersion: (c: ApiContext, id: string, version: number | "latest") =>
    request<TaskVersion>(c, PIPELINE, `/tasks/${e(id)}/versions/${version}`),
  saveTaskVersion: (c: ApiContext, id: string, config: TaskConfig, notes = "") =>
    request<TaskVersion>(c, PIPELINE, `/tasks/${e(id)}/versions`, json("POST", { config, notes })),
  getTaskDraft: (c: ApiContext, id: string) => request<TaskDraft>(c, PIPELINE, `/tasks/${e(id)}/draft`),
  saveTaskDraft: (c: ApiContext, id: string, config: TaskConfig) => request<TaskDraft>(c, PIPELINE, `/tasks/${e(id)}/draft`, json("PUT", { config })),

  // Newest first. `.total` is the exact count matching the filters, so `limit: 1` doubles as a
  // cheap "how many runs have this status" query (the Runs tab's stat cards rely on it).
  listRuns: (c: ApiContext, opts: { pipelineId?: string; status?: RunStatus; limit?: number; offset?: number } = {}) =>
    request<Page<Run>>(c, PIPELINE, `/runs${qs({ pipelineId: opts.pipelineId, status: opts.status, limit: opts.limit ?? 50, offset: opts.offset })}`),
  getRun: (c: ApiContext, id: string) => request<RunDetail>(c, PIPELINE, `/runs/${e(id)}`),
  cancelRun: (c: ApiContext, id: string) => request<Run>(c, PIPELINE, `/runs/${e(id)}/cancel`, { method: "POST" }),
  runLogs: (c: ApiContext, id: string, opts: { task?: string; after?: number } = {}) =>
    request<LogPage>(c, PIPELINE, `/runs/${e(id)}/logs${qs({ task: opts.task, after: opts.after, limit: 1000 })}`),

  // booth-catalog (browse-only). A failure here must never break the builder: inline code needs
  // no catalog at all, so callers surface the error next to the picker and carry on.
  catalogCode: (c: ApiContext, q = "") =>
    request<Page<CatalogCodeEntry>>(c, CATALOG, `/code${qs({ q, language: "python", limit: 25 })}`),
  // The real endpoint has no pagination and wraps its array as {versions: [...]} — NOT the
  // {items, total} shape every other list endpoint in this file uses (confirmed 2026-09-23
  // against booth-catalog's own handleCodeVersions, after a mismatch here crashed the picker on
  // every entry selection; see docs/decisions for the incident).
  catalogVersions: (c: ApiContext, entryId: string) =>
    request<{ versions: CatalogVersionSummary[] }>(c, CATALOG, `/code/${e(entryId)}/versions`).then((r) => r.versions),

  // booth-storage (browse-only). Same "never break the builder" rule as the catalog: a failure
  // here is shown next to the picker, not thrown at the whole page.
  // The real endpoint returns a bare array, not {items: [...]} — confirmed 2026-09-23 against
  // booth-storage's own handleListBackends, after the same wrong assumption crashed the picker
  // the moment "From storage" was selected.
  storageBackends: (c: ApiContext) => request<StorageBackendSummary[]>(c, STORAGE, "/backends"),
  storageObjects: (c: ApiContext, backendId: string, prefix = "") =>
    request<{ entries: StorageObjectSummary[] }>(c, STORAGE, `/backends/${e(backendId)}/objects${qs({ prefix, recursive: "true" })}`),
};
