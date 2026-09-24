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
  ];

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

  it.each(["/", "/pipeline", "/pipeline/nonsense", "/pipeline/pipelines/p1/v/notanumber", "/pipeline/pipelines/p1/extra/parts", "/other/pipelines"])(
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
