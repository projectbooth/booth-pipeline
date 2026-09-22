import type {
  CatalogCodeEntry,
  CatalogVersionSummary,
  Job,
  JobInput,
  LogPage,
  Page,
  Pipeline,
  PipelineSpec,
  PipelineVersion,
  PipelineVersionSummary,
  Run,
  RunDetail,
  RunnerInfo,
  StorageBackendSummary,
  StorageObjectSummary,
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

  listPipelines: (c: ApiContext, q = "") => request<Page<Pipeline>>(c, PIPELINE, `/pipelines${qs({ q, limit: 200 })}`),
  getPipeline: (c: ApiContext, id: string) => request<Pipeline>(c, PIPELINE, `/pipelines/${e(id)}`),
  createPipeline: (c: ApiContext, name: string, description: string) =>
    request<Pipeline>(c, PIPELINE, "/pipelines", json("POST", { name, description })),
  updatePipeline: (c: ApiContext, id: string, name: string, description: string) =>
    request<Pipeline>(c, PIPELINE, `/pipelines/${e(id)}`, json("PUT", { name, description })),
  deletePipeline: (c: ApiContext, id: string) => request<void>(c, PIPELINE, `/pipelines/${e(id)}`, { method: "DELETE" }),
  listVersions: (c: ApiContext, id: string) => request<Page<PipelineVersionSummary>>(c, PIPELINE, `/pipelines/${e(id)}/versions?limit=200`),
  getVersion: (c: ApiContext, id: string, version: number | "latest") =>
    request<PipelineVersion>(c, PIPELINE, `/pipelines/${e(id)}/versions/${version}`),
  saveVersion: (c: ApiContext, id: string, spec: PipelineSpec, notes = "") =>
    request<PipelineVersion>(c, PIPELINE, `/pipelines/${e(id)}/versions`, json("POST", { spec, notes })),
  validate: (c: ApiContext, spec: PipelineSpec) => request<ValidateResult>(c, PIPELINE, "/pipelines/validate", json("POST", { spec })),

  listJobs: (c: ApiContext, pipelineId?: string) => request<Page<Job>>(c, PIPELINE, `/jobs${qs({ pipelineId, limit: 200 })}`),
  getJob: (c: ApiContext, id: string) => request<Job>(c, PIPELINE, `/jobs/${e(id)}`),
  createJob: (c: ApiContext, body: JobInput) => request<Job>(c, PIPELINE, "/jobs", json("POST", body)),
  updateJob: (c: ApiContext, id: string, body: JobInput) => request<Job>(c, PIPELINE, `/jobs/${e(id)}`, json("PUT", body)),
  deleteJob: (c: ApiContext, id: string) => request<void>(c, PIPELINE, `/jobs/${e(id)}`, { method: "DELETE" }),
  runJob: (c: ApiContext, id: string) => request<Run>(c, PIPELINE, `/jobs/${e(id)}/run`, { method: "POST" }),

  listRuns: (c: ApiContext, jobId?: string) => request<Page<Run>>(c, PIPELINE, `/runs${qs({ jobId, limit: 50 })}`),
  getRun: (c: ApiContext, id: string) => request<RunDetail>(c, PIPELINE, `/runs/${e(id)}`),
  cancelRun: (c: ApiContext, id: string) => request<Run>(c, PIPELINE, `/runs/${e(id)}/cancel`, { method: "POST" }),
  runLogs: (c: ApiContext, id: string, opts: { task?: string; after?: number } = {}) =>
    request<LogPage>(c, PIPELINE, `/runs/${e(id)}/logs${qs({ task: opts.task, after: opts.after, limit: 1000 })}`),

  // booth-catalog (browse-only). A failure here must never break the builder: inline code needs
  // no catalog at all, so callers surface the error next to the picker and carry on.
  catalogCode: (c: ApiContext, q = "") =>
    request<Page<CatalogCodeEntry>>(c, CATALOG, `/code${qs({ q, language: "python", limit: 25 })}`),
  catalogVersions: (c: ApiContext, entryId: string) =>
    request<Page<CatalogVersionSummary>>(c, CATALOG, `/code/${e(entryId)}/versions?limit=200`),

  // booth-storage (browse-only). Same "never break the builder" rule as the catalog: a failure
  // here is shown next to the picker, not thrown at the whole page.
  storageBackends: (c: ApiContext) => request<{ items: StorageBackendSummary[] }>(c, STORAGE, "/backends"),
  storageObjects: (c: ApiContext, backendId: string, prefix = "") =>
    request<{ entries: StorageObjectSummary[] }>(c, STORAGE, `/backends/${e(backendId)}/objects${qs({ prefix, recursive: "true" })}`),
};
