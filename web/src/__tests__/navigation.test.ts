import { describe, expect, it } from "vitest";
import { parseRoute, routePath, sectionOf, type Route } from "../navigation";

describe("parseRoute / routePath", () => {
  const routes: Route[] = [
    { name: "pipelines" },
    { name: "pipeline", id: "p1" },
    { name: "pipeline", id: "p1", version: 3 },
    { name: "tasks" },
    { name: "task", id: "t1" },
    { name: "run", id: "r1" },
    { name: "run", id: "r1", task: "extract" },
    { name: "pipeline", id: "p1", tab: "runs" },
    { name: "pipeline", id: "p1", tab: "schedule" },
    { name: "pipeline", id: "p1", tab: "config" },
    { name: "pipeline", id: "p1", tab: "versions" },
    { name: "task", id: "t1", tab: "config" },
    { name: "task", id: "t1", tab: "versions" },
    { name: "task", id: "t1", version: 2 },
    { name: "task", id: "t1", tab: "config", version: 2 },
  ];

  it("gives the default tab no path segment of its own, but still accepts one", () => {
    expect(routePath({ name: "pipeline", id: "p1", tab: "dag" })).toBe("/pipeline/pipelines/p1");
    expect(routePath({ name: "task", id: "t1", tab: "code" })).toBe("/pipeline/tasks/t1");
    expect(parseRoute("/pipeline/pipelines/p1/dag", "")).toEqual({ name: "pipeline", id: "p1" });
    expect(parseRoute("/pipeline/tasks/t1/code", "")).toEqual({ name: "task", id: "t1" });
  });

  it("keeps old /pipelines/:id/v/:n links working", () => {
    expect(parseRoute("/pipeline/pipelines/p1/v/3", "")).toEqual({ name: "pipeline", id: "p1", version: 3 });
  });

  it.each(routes)("round-trips %j", (route) => {
    const path = routePath(route);
    const [pathname, search = ""] = path.split("?");
    expect(parseRoute(pathname, search ? `?${search}` : "")).toEqual(route);
  });

  it("honours a custom base path (the manifest's navPath)", () => {
    expect(routePath({ name: "run", id: "r" }, "/x")).toBe("/x/runs/r");
    expect(parseRoute("/x/runs/r", "", "/x")).toEqual({ name: "run", id: "r" });
  });

  it("encodes and decodes ids safely", () => {
    const p = routePath({ name: "task", id: "a/b c" });
    expect(p).toBe("/pipeline/tasks/a%2Fb%20c");
    expect(parseRoute(p, "")).toEqual({ name: "task", id: "a/b c" });
  });

  it.each(["/", "/pipeline", "/pipeline/nonsense", "/pipeline/pipelines/p1/v/notanumber", "/pipeline/pipelines/p1/extra/parts", "/pipeline/pipelines/p1/bogus", "/other/pipelines"])(
    "an unrecognised path (%s) lands on the pipeline list, not an error",
    (path) => {
      expect(parseRoute(path, "").name).toBe("pipelines");
    },
  );
});

describe("sectionOf", () => {
  it("groups tasks under Tasks, everything else (including runs) under Pipelines", () => {
    expect(sectionOf({ name: "pipelines" })).toBe("pipelines");
    expect(sectionOf({ name: "pipeline", id: "x" })).toBe("pipelines");
    expect(sectionOf({ name: "run", id: "x" })).toBe("pipelines");
    expect(sectionOf({ name: "tasks" })).toBe("tasks");
    expect(sectionOf({ name: "task", id: "x" })).toBe("tasks");
  });
});
