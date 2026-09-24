import { describe, expect, it } from "vitest";
import {
  autoLayout,
  checkConnection,
  connect,
  disconnect,
  edgeId,
  hasOverlappingPositions,
  newRef,
  parseTaskField,
  removeRef,
  renameKey,
  toFlow,
  uniqueKey,
} from "../graph";
import type { TaskRef } from "../types";

function t(key: string, dependsOn: string[] = []): TaskRef {
  return { key, taskId: key, taskVersion: 1, dependsOn, position: { x: 0, y: 0 } };
}

const etl = () => [t("a"), t("b", ["a"]), t("c", ["b"])];

describe("toFlow", () => {
  it("draws one edge per dependency, pointing upstream -> downstream", () => {
    const { nodes, edges } = toFlow([t("a"), t("b", ["a"]), t("c", ["a", "b"])]);
    expect(nodes.map((n) => n.id)).toEqual(["a", "b", "c"]);
    expect(edges).toEqual([
      { id: "a->b", source: "a", target: "b" },
      { id: "a->c", source: "a", target: "c" },
      { id: "b->c", source: "b", target: "c" },
    ]);
  });

  it("carries a task's resolved name, status, error and selection onto the right node only", () => {
    const { nodes } = toFlow(etl(), { taskNames: { a: "Extract" }, statuses: { b: "running" }, errors: { c: "boom" }, selected: "a" });
    const by = Object.fromEntries(nodes.map((n) => [n.id, n]));
    expect(by.a.data.taskName).toBe("Extract");
    expect(by.b.data.status).toBe("running");
    expect(by.c.data.error).toBe("boom");
    expect(by.a.selected).toBe(true);
    expect(by.b.selected).toBe(false);
    expect(by.a.data.status).toBeUndefined();
  });

  it("is a pure function of the references (edge ids round-trip through edgeId)", () => {
    const { edges } = toFlow(etl());
    expect(edges.map((e) => e.id)).toEqual([edgeId("a", "b"), edgeId("b", "c")]);
  });
});

describe("autoLayout", () => {
  it("puts each node one column right of its deepest dependency", () => {
    const laid = autoLayout([t("a"), t("b", ["a"]), t("c", ["a"]), t("d", ["b", "c"])]);
    const x = Object.fromEntries(laid.map((k) => [k.key, k.position.x]));
    expect(x.a).toBe(0);
    expect(x.b).toBeGreaterThan(x.a);
    expect(x.b).toBe(x.c);
    expect(x.d).toBeGreaterThan(x.b);
  });

  it("stacks independent branches instead of overlapping them", () => {
    const laid = autoLayout([t("a"), t("b"), t("c")]);
    expect(new Set(laid.map((k) => k.position.y)).size).toBe(3);
  });

  it("never throws or loops on a cyclic or dangling draft (it runs while the user is mid-edit)", () => {
    expect(() => autoLayout([t("a", ["b"]), t("b", ["a"])])).not.toThrow();
    expect(() => autoLayout([t("a", ["ghost"])])).not.toThrow();
  });

  it("does not mutate its input", () => {
    const input = etl();
    const snapshot = JSON.stringify(input);
    autoLayout(input);
    expect(JSON.stringify(input)).toBe(snapshot);
  });
});

describe("checkConnection (mirrors the server's structural rules)", () => {
  const refs = etl();
  it("allows a legal edge", () => {
    expect(checkConnection([t("a"), t("b")], "a", "b")).toEqual({ ok: true });
  });
  it.each([
    ["a self-loop", "b", "b", /itself/],
    ["a duplicate", "a", "b", /already/],
    ["an unknown task", "a", "zzz", /unknown/],
  ])("refuses %s", (_name, from, to, reason) => {
    const r = checkConnection(refs, from, to);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toMatch(reason);
  });
  it("refuses an edge that would close a cycle, however long the path", () => {
    const chain = [t("s"), t("x", ["s"]), t("y", ["x"]), t("z", ["y"])];
    const r = checkConnection(chain, "z", "x");
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toMatch(/cycle/);
    expect(checkConnection(chain, "x", "z").ok).toBe(true); // a forward shortcut is fine
  });
});

describe("editing", () => {
  it("connect/disconnect touch only the target's dependsOn", () => {
    const base = [t("a"), t("b")];
    const linked = connect(base, "a", "b");
    expect(linked[1].dependsOn).toEqual(["a"]);
    expect(linked[0]).toBe(base[0]);
    expect(disconnect(linked, "a", "b")[1].dependsOn).toEqual([]);
  });

  it("removing a reference also removes every dependency on it", () => {
    const after = removeRef([t("a"), t("b", ["a"]), t("c", ["a", "b"])], "a");
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

describe("new references", () => {
  it("get unique, valid keys even after deletions", () => {
    expect(uniqueKey("task", [])).toBe("task_1");
    expect(uniqueKey("task", ["task_1", "task_2"])).toBe("task_3");
    expect(uniqueKey("task", ["task_2"])).toBe("task_1");
  });

  it("references the given task at its latest version, with an empty wiring", () => {
    const ref = newRef("t1", []);
    expect(ref.taskId).toBe("t1");
    expect(ref.taskVersion).toBe("latest");
    expect(ref.dependsOn).toEqual([]);
    expect(ref.key).toMatch(/^task_\d+$/);
  });

  it("are placed below what is already on the canvas", () => {
    const existing = autoLayout(etl());
    const fresh = newRef("t9", existing);
    expect(fresh.position.y).toBeGreaterThan(Math.max(...existing.map((k) => k.position.y)));
  });
});

describe("hasOverlappingPositions", () => {
  const at = (key: string, x: number, y: number): TaskRef => ({ ...t(key), position: { x, y } });

  it("is true when every reference shares the server's {0,0} default", () => {
    expect(hasOverlappingPositions([at("a", 0, 0), at("b", 0, 0)])).toBe(true);
  });

  it("is true for a partial overlap — even one pair stacked is a real bug", () => {
    expect(hasOverlappingPositions([at("a", 0, 0), at("b", 0, 0), at("c", 300, 0)])).toBe(true);
  });

  it("is false once every reference has been laid out to a distinct spot", () => {
    expect(hasOverlappingPositions(autoLayout([at("a", 0, 0), at("b", 0, 0), at("c", 0, 0)]))).toBe(false);
    expect(hasOverlappingPositions([at("a", 0, 0), at("b", 300, 0), at("c", 0, 200)])).toBe(false);
  });

  it("is false for zero or one reference — nothing to overlap", () => {
    expect(hasOverlappingPositions([])).toBe(false);
    expect(hasOverlappingPositions([at("a", 0, 0)])).toBe(false);
  });
});

describe("parseTaskField", () => {
  it.each([
    ["tasks[2].dependsOn", { index: 2, rest: "dependsOn" }],
    ["spec.tasks[0].taskId", { index: 0, rest: "taskId" }],
    ["tasks[1]", { index: 1, rest: null }],
    ["tasks", null],
    ["name", null],
    [undefined, null],
    [null, null],
  ] as [string | null | undefined, { index: number; rest: string | null } | null][])("%s -> %s", (field, want) => {
    expect(parseTaskField(field)).toEqual(want);
  });
});
