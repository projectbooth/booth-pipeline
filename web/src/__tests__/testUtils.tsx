import { vi } from "vitest";
import type { Page, Pipeline, PipelineDraft, PipelineVersion, RunDetail, TaskConfig, TaskDraft, TaskEntity, TaskRef, TaskVersion } from "../types";

// A tiny in-memory stand-in for the pipeline backend + the catalog behind booth-core's gateway,
// installed as the global `fetch`. Views are exercised against it exactly as against the real
// thing: same paths, same JSON, same error envelope.

export interface Call {
  method: string;
  path: string;
  body: unknown;
  headers: Headers;
}

const now = "2026-09-21T12:00:00+00:00";

function defaultConfig(over: Partial<TaskConfig> = {}): TaskConfig {
  return {
    code: { type: "inline", source: "def run(ctx):\n    return 1\n" },
    runner: "base",
    retry: null,
    params: {},
    timeoutSeconds: 3600,
    platformAccess: false,
    ...over,
  };
}

export class FakeBackend {
  calls: Call[] = [];
  pipelines: Pipeline[] = [];
  versions = new Map<string, PipelineVersion[]>();
  pipelineDrafts = new Map<string, PipelineDraft>();
  tasks: TaskEntity[] = [];
  taskVersions = new Map<string, TaskVersion[]>();
  taskDrafts = new Map<string, TaskDraft>();
  runs = new Map<string, RunDetail>();
  logs = new Map<string, { seq: number; taskKey: string | null; stream: string; message: string; attempt?: number }[]>();
  /** Errors to return for the next call whose "METHOD path" matches. */
  failNext = new Map<string, { status: number; error: string; field?: string }>();
  catalogDown = false;
  catalogEntries = [{ id: "e1", name: "loader", description: "loads things", language: "python", latestVersion: { version: "2.0.0" }, versionCount: 2 }];
  storageDown = false;
  storageBackends = [{ id: "b1", displayName: "Main" }];
  storageObjects: Record<string, string[]> = { b1: ["tasks/a.py", "tasks/b.sql"] };
  runners = [
    { id: "base", displayName: "Base", available: true, reason: null },
    { id: "spark", displayName: "Spark", available: false, reason: "booth-spark does not yet define a compute-submission interface" },
  ];

  addPipeline(name: string, refs?: TaskRef[]): Pipeline {
    const p: Pipeline = {
      id: `p${this.pipelines.length + 1}`,
      name,
      description: "",
      createdBy: "eddie",
      createdAt: now,
      updatedAt: now,
      latestVersion: 0,
      schedule: null,
      allowConcurrentRuns: false,
      roleCeiling: "editor",
      hasOwner: false,
      nextRunAt: null,
      pinnedVersion: null,
      hasDraft: false,
      draftUpdatedAt: null,
    };
    this.pipelines.push(p);
    this.versions.set(p.id, []);
    if (refs) this.saveVersion(p.id, refs);
    return p;
  }

  saveVersion(id: string, tasks: TaskRef[], notes = ""): PipelineVersion {
    const list = this.versions.get(id)!;
    const v: PipelineVersion = { pipelineId: id, version: list.length + 1, notes, createdBy: "eddie", createdAt: now, taskCount: tasks.length, spec: { tasks } };
    list.push(v);
    this.pipelines.find((p) => p.id === id)!.latestVersion = v.version;
    this.savePipelineDraft(id, tasks); // ADR 0073: a version-save also refreshes the draft
    return v;
  }

  savePipelineDraft(id: string, tasks: TaskRef[]): PipelineDraft {
    const d: PipelineDraft = { pipelineId: id, spec: { tasks }, updatedBy: "eddie", updatedAt: now };
    this.pipelineDrafts.set(id, d);
    this.pipelines.find((p) => p.id === id)!.hasDraft = true;
    return d;
  }

  addTask(name: string, config?: TaskConfig, description = ""): TaskEntity {
    const t: TaskEntity = { id: `t${this.tasks.length + 1}`, name, description, createdBy: "eddie", createdAt: now, updatedAt: now, latestVersion: 0, hasDraft: false, draftUpdatedAt: null };
    this.tasks.push(t);
    this.taskVersions.set(t.id, []);
    if (config) this.saveTaskVersion(t.id, config);
    return t;
  }

  /** A DAG-node reference to a freshly created standalone task (ADR 0071) — the test-only
   *  equivalent of the old embedded `task()` builder, split across a real task and its ref. */
  ref(key: string, dependsOn: string[] = [], configOver: Partial<TaskConfig> = {}): TaskRef {
    const t = this.addTask(key, defaultConfig(configOver));
    return { key, taskId: t.id, taskVersion: 1, dependsOn, position: { x: 0, y: 0 } };
  }

  saveTaskVersion(id: string, config: TaskConfig, notes = ""): TaskVersion {
    const list = this.taskVersions.get(id)!;
    const v: TaskVersion = { taskId: id, version: list.length + 1, notes, createdBy: "eddie", createdAt: now, config };
    list.push(v);
    this.tasks.find((t) => t.id === id)!.latestVersion = v.version;
    this.saveTaskDraft(id, config); // ADR 0073: a version-save also refreshes the draft
    return v;
  }

  saveTaskDraft(id: string, config: TaskConfig): TaskDraft {
    const d: TaskDraft = { taskId: id, config, updatedBy: "eddie", updatedAt: now };
    this.taskDrafts.set(id, d);
    this.tasks.find((t) => t.id === id)!.hasDraft = true;
    return d;
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
      // Real shape (booth-catalog's handleCodeVersions): {"versions": [...]}, no "items"/"total".
      if (/^\/code\/[^/]+\/versions$/.test(path)) return this.json({ versions: [{ version: "2.0.0", seq: 2, notes: "" }, { version: "1.0.0", seq: 1, notes: "" }] });
    }

    if (isStorage) {
      if (this.storageDown) return new Response("module not found", { status: 404 });
      // Real shape (booth-storage's handleListBackends): a bare array, no "items" wrapper.
      if (path === "/backends") return this.json(this.storageBackends);
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
    if ((m = path.match(/^\/pipelines\/([^/]+)\/schedule$/)) && method === "PUT") {
      const p = this.pipelines.find((x) => x.id === m![1])!;
      Object.assign(p, body, { hasOwner: true, nextRunAt: body.schedule?.enabled ? now : null });
      return this.json(p);
    }
    if ((m = path.match(/^\/pipelines\/([^/]+)\/run$/)) && method === "POST") {
      const r = this.newRun(m[1]);
      return this.json(r, 202);
    }
    if ((m = path.match(/^\/pipelines\/([^/]+)\/versions$/))) {
      if (method === "GET") return this.json(this.page((this.versions.get(m[1]) ?? []).slice().reverse().map((v) => ({ ...v, spec: undefined }))));
      if (method === "POST") return this.json(this.saveVersion(m[1], body.spec.tasks, body.notes), 201);
    }
    if ((m = path.match(/^\/pipelines\/([^/]+)\/draft$/))) {
      if (method === "GET") {
        const d = this.pipelineDrafts.get(m[1]);
        return d ? this.json(d) : this.json({ error: "no draft saved for this pipeline" }, 404);
      }
      if (method === "PUT") return this.json(this.savePipelineDraft(m[1], body.spec.tasks));
    }
    if ((m = path.match(/^\/pipelines\/([^/]+)\/versions\/(.+)\/export$/)) && method === "GET") {
      const list = this.versions.get(m[1]) ?? [];
      const v = m[2] === "latest" ? list.at(-1) : list[Number(m[2]) - 1];
      if (!v) return this.json({ error: "pipeline version not found" }, 404);
      const yamlText = `apiVersion: booth-pipeline/v1\nkind: Pipeline\nmetadata:\n  id: ${m[1]}\n  version: ${v.version}\n`;
      return new Response(yamlText, { status: 200, headers: { "Content-Type": "application/yaml" } });
    }
    if ((m = path.match(/^\/pipelines\/([^/]+)\/versions\/(.+)$/)) && method === "GET") {
      const list = this.versions.get(m[1]) ?? [];
      const v = m[2] === "latest" ? list.at(-1) : list[Number(m[2]) - 1];
      return v ? this.json(v) : this.json({ error: "pipeline version not found" }, 404);
    }
    if (method === "GET" && path === "/tasks") {
      const q = (u.searchParams.get("q") ?? "").trim().toLowerCase();
      const items = q ? this.tasks.filter((t) => t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q)) : this.tasks;
      return this.json(this.page(items));
    }
    if (method === "POST" && path === "/tasks") {
      const t = this.addTask(body.name, body.config, body.description ?? "");
      const version = t.latestVersion > 0 ? this.taskVersions.get(t.id)!.at(-1) : null;
      return this.json({ ...t, version }, 201);
    }
    if ((m = path.match(/^\/tasks\/([^/]+)$/))) {
      const t = this.tasks.find((x) => x.id === m![1]);
      if (!t) return this.json({ error: "task not found" }, 404);
      if (method === "GET") return this.json(t);
      if (method === "PUT") return this.json(Object.assign(t, { name: body.name, description: body.description }));
      if (method === "DELETE") {
        this.tasks = this.tasks.filter((x) => x.id !== t.id);
        return this.json(undefined, 204);
      }
    }
    if ((m = path.match(/^\/tasks\/([^/]+)\/versions$/))) {
      if (method === "GET") return this.json(this.page((this.taskVersions.get(m[1]) ?? []).slice().reverse().map((v) => ({ ...v, config: undefined }))));
      if (method === "POST") return this.json(this.saveTaskVersion(m[1], body.config, body.notes), 201);
    }
    if ((m = path.match(/^\/tasks\/([^/]+)\/versions\/(.+)$/)) && method === "GET") {
      const list = this.taskVersions.get(m[1]) ?? [];
      const v = m[2] === "latest" ? list.at(-1) : list[Number(m[2]) - 1];
      return v ? this.json(v) : this.json({ error: "task version not found" }, 404);
    }
    if ((m = path.match(/^\/tasks\/([^/]+)\/draft$/))) {
      if (method === "GET") {
        const d = this.taskDrafts.get(m[1]);
        return d ? this.json(d) : this.json({ error: "no draft saved for this task" }, 404);
      }
      if (method === "PUT") return this.json(this.saveTaskDraft(m[1], body.config));
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

  newRun(pipelineId: string): RunDetail {
    const pipeline = this.pipelines.find((p) => p.id === pipelineId)!;
    const r: RunDetail = {
      id: `run-${this.runs.size + 1}-abcdef`,
      pipelineId,
      pipelineVersion: pipeline.pinnedVersion ?? pipeline.latestVersion,
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
