import { describe, expect, it } from "vitest";
import {
  autoLayout,
  checkConnection,
  connect,
  disconnect,
  edgeId,
  newTask,
  removeTask,
  renameKey,
  taskIndexOfField,
  toFlow,
  uniqueKey,
} from "../graph";
import type { Task, TaskKind } from "../types";

function t(key: string, kind: TaskKind = "transform", dependsOn: string[] = []): Task {
  return { key, name: "", kind, code: { type: "inline", source: "x" }, runner: "base", retry: null, dependsOn, params: {}, timeoutSeconds: 60, platformAccess: false, position: { x: 0, y: 0 } };
}

const etl = () => [t("a", "source"), t("b", "transform", ["a"]), t("c", "sink", ["b"])];

describe("toFlow", () => {
  it("draws one edge per dependency, pointing upstream -> downstream", () => {
    const { nodes, edges } = toFlow([t("a", "source"), t("b", "transform", ["a"]), t("c", "sink", ["a", "b"])]);
    expect(nodes.map((n) => n.id)).toEqual(["a", "b", "c"]);
    expect(edges).toEqual([
      { id: "a->b", source: "a", target: "b" },
      { id: "a->c", source: "a", target: "c" },
      { id: "b->c", source: "b", target: "c" },
    ]);
  });

  it("carries status, error and selection onto the right node only", () => {
    const { nodes } = toFlow(etl(), { statuses: { b: "running" }, errors: { c: "boom" }, selected: "a" });
    const by = Object.fromEntries(nodes.map((n) => [n.id, n]));
    expect(by.b.data.status).toBe("running");
    expect(by.c.data.error).toBe("boom");
    expect(by.a.selected).toBe(true);
    expect(by.b.selected).toBe(false);
    expect(by.a.data.status).toBeUndefined();
  });

  it("is a pure function of the tasks (edge ids round-trip through edgeId)", () => {
    const { edges } = toFlow(etl());
    expect(edges.map((e) => e.id)).toEqual([edgeId("a", "b"), edgeId("b", "c")]);
  });
});

describe("autoLayout", () => {
  it("puts each task one column right of its deepest dependency", () => {
    const laid = autoLayout([t("a", "source"), t("b", "transform", ["a"]), t("c", "transform", ["a"]), t("d", "sink", ["b", "c"])]);
    const x = Object.fromEntries(laid.map((k) => [k.key, k.position.x]));
    expect(x.a).toBe(0);
    expect(x.b).toBeGreaterThan(x.a);
    expect(x.b).toBe(x.c);
    expect(x.d).toBeGreaterThan(x.b);
  });

  it("stacks independent branches instead of overlapping them", () => {
    const laid = autoLayout([t("a", "source"), t("b", "source"), t("c", "source")]);
    expect(new Set(laid.map((k) => k.position.y)).size).toBe(3);
  });

  it("never throws or loops on a cyclic or dangling draft (it runs while the user is mid-edit)", () => {
    expect(() => autoLayout([t("a", "transform", ["b"]), t("b", "transform", ["a"])])).not.toThrow();
    expect(() => autoLayout([t("a", "transform", ["ghost"])])).not.toThrow();
  });

  it("does not mutate its input", () => {
    const input = etl();
    const snapshot = JSON.stringify(input);
    autoLayout(input);
    expect(JSON.stringify(input)).toBe(snapshot);
  });
});

describe("checkConnection (mirrors the server's structural rules)", () => {
  const tasks = etl();
  it("allows a legal edge", () => {
    expect(checkConnection([t("a", "source"), t("b", "transform")], "a", "b")).toEqual({ ok: true });
  });
  it.each([
    ["a self-loop", "b", "b", /itself/],
    ["an edge INTO a source", "b", "a", /source/],
    ["an edge OUT OF a sink", "c", "b", /sink/],
    ["a duplicate", "a", "b", /already/],
    ["an unknown task", "a", "zzz", /unknown/],
  ])("refuses %s", (_name, from, to, reason) => {
    const r = checkConnection(tasks, from, to);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toMatch(reason);
  });
  it("refuses an edge that would close a cycle, however long the path", () => {
    const chain = [t("s", "source"), t("x", "transform", ["s"]), t("y", "transform", ["x"]), t("z", "transform", ["y"])];
    const r = checkConnection(chain, "z", "x");
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toMatch(/cycle/);
    expect(checkConnection(chain, "x", "z").ok).toBe(true); // a forward shortcut is fine
  });
});

describe("editing", () => {
  it("connect/disconnect touch only the target's dependsOn", () => {
    const base = [t("a", "source"), t("b", "transform")];
    const linked = connect(base, "a", "b");
    expect(linked[1].dependsOn).toEqual(["a"]);
    expect(linked[0]).toBe(base[0]);
    expect(disconnect(linked, "a", "b")[1].dependsOn).toEqual([]);
  });

  it("removing a task also removes every reference to it", () => {
    const after = removeTask([t("a", "source"), t("b", "transform", ["a"]), t("c", "sink", ["a", "b"])], "a");
    expect(after.map((k) => k.key)).toEqual(["b", "c"]);
    expect(after.every((k) => !k.dependsOn.includes("a"))).toBe(true);
    expect(after[1].dependsOn).toEqual(["b"]);
  });

  it("renaming a key rewrites every dependency on it", () => {
    const after = renameKey(etl(), "b", "clean");
    expect(after.map((k) => k.key)).toEqual(["a", "clean", "c"]);
    expect(after[2].dependsOn).toEqual(["clean"]);
    expect(after[1].dependsOn).toEqual(["a"]);
  });
});

describe("new tasks", () => {
  it("get unique, valid keys even after deletions", () => {
    expect(uniqueKey("source", [])).toBe("source_1");
    expect(uniqueKey("source", ["source_1", "source_2"])).toBe("source_3");
    expect(uniqueKey("source", ["source_2"])).toBe("source_1");
  });

  it.each<TaskKind>(["source", "transform", "sink"])("a new %s starts with runnable, base-runner, inline code", (kind) => {
    const task = newTask(kind, []);
    expect(task.kind).toBe(kind);
    expect(task.runner).toBe("base"); // ADR 0006: the base runner is the default, never Spark
    expect(task.code.type === "inline" && task.code.source.includes("def run(ctx)")).toBe(true);
    expect(task.dependsOn).toEqual([]);
    expect(task.key).toMatch(/^[a-z][a-z0-9_]{0,62}$/);
  });

  it("are placed below what is already on the canvas", () => {
    const existing = autoLayout(etl());
    const fresh = newTask("source", existing);
    expect(fresh.position.y).toBeGreaterThan(Math.max(...existing.map((k) => k.position.y)));
  });
});

describe("taskIndexOfField", () => {
  it.each([
    ["tasks[2].dependsOn", 2],
    ["spec.tasks[0].retry.maxRetries", 0],
    ["tasks", null],
    ["name", null],
    [undefined, null],
    [null, null],
  ])("%s -> %s", (field, want) => {
    expect(taskIndexOfField(field as string | null | undefined)).toBe(want);
  });
});
