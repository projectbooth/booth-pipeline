import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, type ApiContext } from "../api/client";

let fetchMock: ReturnType<typeof vi.fn>;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  fetchMock = vi.fn(async () => json({ items: [], total: 0 }));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const ctx = (token: string | null = "tok-1"): ApiContext => ({ workspace: "acme", getAccessToken: () => token });
const lastCall = () => {
  const [url, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit];
  return { url, init, headers: new Headers(init.headers) };
};

describe("request plumbing (ADR 0031/0033)", () => {
  it("goes through booth-core's gateway prefix, never a bare /api", async () => {
    await api.listPipelines(ctx());
    expect(lastCall().url.startsWith("/modules/pipeline/api/pipelines")).toBe(true);
  });

  it("sets X-Workspace and the bearer token on every request", async () => {
    await api.listTasks(ctx());
    expect(lastCall().headers.get("X-Workspace")).toBe("acme");
    expect(lastCall().headers.get("Authorization")).toBe("Bearer tok-1");
  });

  it("omits Authorization entirely when there is no token — never the literal string 'null'", async () => {
    await api.listTasks(ctx(null));
    expect(lastCall().headers.has("Authorization")).toBe(false);
  });

  it("asks for the token freshly before every request (it can be silently renewed)", async () => {
    let n = 0;
    const c: ApiContext = { workspace: "acme", getAccessToken: () => `tok-${++n}` };
    await api.listTasks(c);
    await api.listTasks(c);
    expect(fetchMock.mock.calls.map(([, init]) => new Headers((init as RequestInit).headers).get("Authorization"))).toEqual(["Bearer tok-1", "Bearer tok-2"]);
  });

  it("url-encodes ids so a hostile id cannot escape its path segment", async () => {
    await api.getPipeline(ctx(), "a/b?c#d");
    expect(lastCall().url).toBe("/modules/pipeline/api/pipelines/a%2Fb%3Fc%23d");
  });
});

describe("endpoints", () => {
  it("saves a version with the spec and notes as JSON", async () => {
    fetchMock.mockResolvedValueOnce(json({ version: 3 }));
    await api.saveVersion(ctx(), "p1", { tasks: [] }, "why");
    const { url, init, headers } = lastCall();
    expect(url).toBe("/modules/pipeline/api/pipelines/p1/versions");
    expect(init.method).toBe("POST");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({ spec: { tasks: [] }, notes: "why" });
  });

  it("run logs use the seq cursor and task filter", async () => {
    fetchMock.mockResolvedValueOnce(json({ items: [], done: false }));
    await api.runLogs(ctx(), "r1", { task: "load", after: 42 });
    const u = new URL(lastCall().url, "http://x");
    expect(u.pathname).toBe("/modules/pipeline/api/runs/r1/logs");
    expect(u.searchParams.get("task")).toBe("load");
    expect(u.searchParams.get("after")).toBe("42");
  });

  it("omits empty query parameters", async () => {
    await api.listPipelines(ctx(), "");
    expect(lastCall().url).not.toContain("q=");
  });

  it("calls the catalog through ITS gateway prefix, browse-only", async () => {
    await api.catalogCode(ctx(), "loader");
    const u = new URL(lastCall().url, "http://x");
    expect(u.pathname).toBe("/modules/catalog/api/code");
    expect(u.searchParams.get("q")).toBe("loader");
    expect(u.searchParams.get("language")).toBe("python");
    expect(lastCall().init.method).toBeUndefined();
  });

  it("calls storage through ITS gateway prefix, browse-only, recursively (ADR 0063)", async () => {
    await api.storageObjects(ctx(), "b1", "tasks/");
    const u = new URL(lastCall().url, "http://x");
    expect(u.pathname).toBe("/modules/storage/api/backends/b1/objects");
    expect(u.searchParams.get("prefix")).toBe("tasks/");
    expect(u.searchParams.get("recursive")).toBe("true");
    expect(lastCall().init.method).toBeUndefined();
  });

  // Regression for the 2026-09-23 pilot finding: these two endpoints' real response shapes
  // (confirmed against booth-catalog's/booth-storage's own Go source, not this repo's fakes,
  // which had independently — and wrongly — assumed the same {items: [...]} shape every other
  // list endpoint here uses) differ from every other list endpoint in this file, and a mismatch
  // crashed the picker the moment it loaded real data.
  it("parses booth-catalog's real versions shape — {versions: [...]}, no items/total", async () => {
    fetchMock.mockResolvedValueOnce(json({ versions: [{ version: "1.0.0", seq: 1, notes: "" }] }));
    await expect(api.catalogVersions(ctx(), "e1")).resolves.toEqual([{ version: "1.0.0", seq: 1, notes: "" }]);
  });

  it("parses booth-storage's real backends shape — a bare array, not {items: [...]}", async () => {
    fetchMock.mockResolvedValueOnce(json([{ id: "b1", displayName: "Main" }]));
    await expect(api.storageBackends(ctx())).resolves.toEqual([{ id: "b1", displayName: "Main" }]);
  });

  it("unwraps runners", async () => {
    fetchMock.mockResolvedValueOnce(json({ items: [{ id: "base", displayName: "Base", available: true, reason: null }] }));
    expect((await api.runners(ctx()))[0].id).toBe("base");
  });

  it("204 resolves to undefined", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await expect(api.deleteTask(ctx(), "t")).resolves.toBeUndefined();
  });
});

describe("errors", () => {
  it("surfaces the server's message and the field it is about", async () => {
    fetchMock.mockResolvedValueOnce(json({ error: "b depends on nope", field: "tasks[1].dependsOn" }, 422));
    const err = await api.saveVersion(ctx(), "p", { tasks: [] }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 422, message: "b depends on nope", field: "tasks[1].dependsOn" });
  });

  it("falls back to the raw body for a non-JSON gateway error page", async () => {
    fetchMock.mockResolvedValueOnce(new Response("module not found", { status: 404 }));
    await expect(api.catalogCode(ctx())).rejects.toMatchObject({ status: 404, message: "module not found" });
  });
});
