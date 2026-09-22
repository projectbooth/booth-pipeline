import { vi } from "vitest";
import type { Job, Page, Pipeline, PipelineVersion, RunDetail, Task } from "../types";

// A tiny in-memory stand-in for the pipeline backend + the catalog behind booth-core's gateway,
// installed as the global `fetch`. Views are exercised against it exactly as against the real
// thing: same paths, same JSON, same error envelope.

export interface Call {
  method: string;
  path: string;
  body: unknown;
  headers: Headers;
}

export function task(key: string, kind: Task["kind"] = "transform", dependsOn: string[] = [], over: Partial<Task> = {}): Task {
  return {
    key,
    name: "",
    kind,
    code: { type: "inline", source: "def run(ctx):\n    return 1\n" },
    runner: "base",
    retry: null,
    dependsOn,
    params: {},
    timeoutSeconds: 3600,
    platformAccess: false,
    position: { x: 0, y: 0 },
    ...over,
  };
}

export const ETL = (): Task[] => [task("extract", "source"), task("clean", "transform", ["extract"]), task("load", "sink", ["clean"])];

const now = "2026-09-21T12:00:00+00:00";

export class FakeBackend {
  calls: Call[] = [];
  pipelines: Pipeline[] = [];
  versions = new Map<string, PipelineVersion[]>();
  jobs: Job[] = [];
  runs = new Map<string, RunDetail>();
  logs = new Map<string, { seq: number; taskKey: string | null; stream: string; message: string; attempt?: number }[]>();
  /** Errors to return for the next call whose "METHOD path" matches. */
  failNext = new Map<string, { status: number; error: string; field?: string }>();
  catalogDown = false;
  catalogEntries = [{ id: "e1", name: "loader", description: "loads things", language: "python", latestVersion: { version: "2.0.0" }, versionCount: 2 }];
  storageDown = false;
  storageBackends = [{ id: "b1", name: "Main" }];
  storageObjects: Record<string, string[]> = { b1: ["tasks/a.py", "tasks/b.sql"] };
  runners = [
    { id: "base", displayName: "Base", available: true, reason: null },
    { id: "spark", displayName: "Spark", available: false, reason: "booth-spark does not yet define a compute-submission interface" },
  ];

  addPipeline(name: string, tasks?: Task[]): Pipeline {
    const p: Pipeline = { id: `p${this.pipelines.length + 1}`, name, description: "", createdBy: "eddie", createdAt: now, updatedAt: now, latestVersion: 0 };
    this.pipelines.push(p);
    this.versions.set(p.id, []);
    if (tasks) this.saveVersion(p.id, tasks);
    return p;
  }

  saveVersion(id: string, tasks: Task[], notes = ""): PipelineVersion {
    const list = this.versions.get(id)!;
    const v: PipelineVersion = { pipelineId: id, version: list.length + 1, notes, createdBy: "eddie", createdAt: now, taskCount: tasks.length, spec: { tasks } };
    list.push(v);
    this.pipelines.find((p) => p.id === id)!.latestVersion = v.version;
    return v;
  }

  install(): void {
    vi.stubGlobal("fetch", vi.fn((url: string, init: RequestInit = {}) => Promise.resolve(this.handle(url, init))));
  }

  private json(body: unknown, status = 200): Response {
    return new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }

  called(method: string, pathPrefix: string): Call[] {
    return this.calls.filter((c) => c.method === method && c.path.startsWith(pathPrefix));
  }

  private handle(url: string, init: RequestInit): Response {
    const u = new URL(url, "http://shell");
    const method = (init.method ?? "GET").toUpperCase();
    const body = init.body ? JSON.parse(init.body as string) : undefined;
    const path = u.pathname.replace(/^\/modules\/(pipeline|catalog|storage)\/api/, "");
    const isCatalog = u.pathname.startsWith("/modules/catalog");
    const isStorage = u.pathname.startsWith("/modules/storage");
    const prefix = isCatalog ? "catalog:" : isStorage ? "storage:" : "";
    this.calls.push({ method, path: prefix + path + u.search, body, headers: new Headers(init.headers) });

    const key = `${method} ${prefix + path}`;
    const fail = this.failNext.get(key);
    if (fail) {
      this.failNext.delete(key);
      return this.json({ error: fail.error, ...(fail.field ? { field: fail.field } : {}) }, fail.status);
    }

    if (isCatalog) {
      if (this.catalogDown) return new Response("module not found", { status: 404 });
      if (path === "/code") return this.json({ items: this.catalogEntries, total: this.catalogEntries.length });
      if (/^\/code\/[^/]+\/versions$/.test(path)) return this.json({ items: [{ version: "2.0.0", seq: 2, notes: "" }, { version: "1.0.0", seq: 1, notes: "" }], total: 2 });
    }

    if (isStorage) {
      if (this.storageDown) return new Response("module not found", { status: 404 });
      if (path === "/backends") return this.json({ items: this.storageBackends });
      const om = path.match(/^\/backends\/([^/]+)\/objects$/);
      if (om) {
        const wantPrefix = u.searchParams.get("prefix") ?? "";
        const paths = (this.storageObjects[om[1]] ?? []).filter((p) => p.startsWith(wantPrefix));
        return this.json({ entries: paths.map((p) => ({ path: p })) });
      }
    }

    let m: RegExpMatchArray | null;
    if (method === "GET" && path === "/runners") return this.json({ items: this.runners });
    if (method === "GET" && path === "/pipelines") return this.json(this.page(this.pipelines));
    if (method === "POST" && path === "/pipelines") {
      const p = this.addPipeline(body.name);
      p.description = body.description ?? "";
      return this.json(p, 201);
    }
    if (method === "POST" && path === "/pipelines/validate") return this.json({ valid: true });
    if ((m = path.match(/^\/pipelines\/([^/]+)$/)) && method === "GET") {
      const p = this.pipelines.find((x) => x.id === m![1]);
      return p ? this.json(p) : this.json({ error: "pipeline not found" }, 404);
    }
    if ((m = path.match(/^\/pipelines\/([^/]+)\/versions$/))) {
      if (method === "GET") return this.json(this.page((this.versions.get(m[1]) ?? []).slice().reverse().map((v) => ({ ...v, spec: undefined }))));
      if (method === "POST") return this.json(this.saveVersion(m[1], body.spec.tasks, body.notes), 201);
    }
    if ((m = path.match(/^\/pipelines\/([^/]+)\/versions\/(.+)$/)) && method === "GET") {
      const list = this.versions.get(m[1]) ?? [];
      const v = m[2] === "latest" ? list.at(-1) : list[Number(m[2]) - 1];
      return v ? this.json(v) : this.json({ error: "pipeline version not found" }, 404);
    }
    if (method === "GET" && path === "/jobs") return this.json(this.page(this.jobs));
    if (method === "POST" && path === "/jobs") {
      const j: Job = { id: `j${this.jobs.length + 1}`, createdBy: "eddie", createdAt: now, updatedAt: now, nextRunAt: null, ...body };
      this.jobs.push(j);
      return this.json(j, 201);
    }
    if ((m = path.match(/^\/jobs\/([^/]+)$/))) {
      const j = this.jobs.find((x) => x.id === m![1]);
      if (!j) return this.json({ error: "job not found" }, 404);
      if (method === "GET") return this.json(j);
      if (method === "PUT") return this.json(Object.assign(j, body));
    }
    if ((m = path.match(/^\/jobs\/([^/]+)\/run$/)) && method === "POST") {
      const r = this.newRun(m[1]);
      return this.json(r, 202);
    }
    if (method === "GET" && path === "/runs") return this.json(this.page([...this.runs.values()]));
    if ((m = path.match(/^\/runs\/([^/]+)$/)) && method === "GET") {
      const r = this.runs.get(m[1]);
      return r ? this.json(r) : this.json({ error: "run not found" }, 404);
    }
    if ((m = path.match(/^\/runs\/([^/]+)\/cancel$/)) && method === "POST") {
      const r = this.runs.get(m[1])!;
      r.cancelRequested = true;
      return this.json(r, 202);
    }
    if ((m = path.match(/^\/runs\/([^/]+)\/logs$/)) && method === "GET") {
      const all = this.logs.get(m[1]) ?? [];
      const after = Number(u.searchParams.get("after") ?? 0);
      const task = u.searchParams.get("task");
      const items = all.filter((l) => l.seq > after && (!task || l.taskKey === task)).map((l) => ({ ts: now, attempt: 1, ...l }));
      const run = this.runs.get(m[1]);
      return this.json({ items, done: !!run && !["queued", "running"].includes(run.status) });
    }
    return this.json({ error: `fake backend: unhandled ${method} ${path}` }, 501);
  }

  newRun(jobId: string): RunDetail {
    const job = this.jobs.find((j) => j.id === jobId)!;
    const r: RunDetail = {
      id: `run-${this.runs.size + 1}-abcdef`,
      jobId,
      pipelineId: job.pipelineId,
      pipelineVersion: job.pipelineVersion ?? this.pipelines.find((p) => p.id === job.pipelineId)!.latestVersion,
      status: "running",
      trigger: "manual",
      triggeredBy: "eddie",
      createdAt: now,
      startedAt: now,
      finishedAt: null,
      error: null,
      cancelRequested: false,
      tasks: [],
    };
    this.runs.set(r.id, r);
    return r;
  }

  private page<T>(items: T[]): Page<T> {
    return { items, total: items.length };
  }
}

export const getToken = () => "tok-test";
