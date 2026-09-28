import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { effectiveParams, overridesLabel } from "../dag/ParamOverrides";
import { duplicateRef } from "../graph";
import type { TaskRef } from "../types";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// ADR 0078: a DAG node's own param overrides, set from the node editor below the canvas.

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
});
afterEach(() => vi.unstubAllGlobals());

/** One `fetch_quote` task with its own default params, referenced by one node. */
function quotes() {
  const hood = b.ref("hood", [], { params: { symbol: "BASE", exchange: "NASDAQ" } });
  const p = b.addPipeline("quotes", [hood]);
  return { p, hood };
}

const path = (id: string) => `/pipeline/pipelines/${id}`;
const clickNode = async (key: string) => fireEvent.click(await screen.findByTestId(`task-node-${key}`));
const toolbar = () => screen.getByRole("toolbar", { name: "Pipeline builder" });
const setOverrides = (text: string) => fireEvent.change(screen.getByLabelText("This node's overrides (JSON)"), { target: { value: text } });

describe("a node's param overrides", () => {
  it("shows the task's defaults, takes this node's overrides, previews the merge, and labels the node", async () => {
    const { p } = quotes();
    renderApp(b, { path: path(p.id) });
    await clickNode("hood");
    const section = await screen.findByRole("region", { name: "Parameters for this node" });
    await waitFor(() => expect(within(section).getByLabelText("Task's own params")).toHaveTextContent('"symbol": "BASE"'));

    setOverrides('{"symbol": "HOOD"}');
    const effective = within(section).getByLabelText("Effective params");
    expect(effective).toHaveTextContent('symbol: "HOOD" (this node)');
    expect(effective).toHaveTextContent('exchange: "NASDAQ"'); // the task's default still applies
    expect(screen.getByTestId("task-node-overrides-hood")).toHaveTextContent("symbol=HOOD");
    expect(within(toolbar()).getByText("Unsaved changes")).toBeInTheDocument();

    await userEvent.click(within(toolbar()).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(b.called("PUT", `/pipelines/${p.id}/draft`)).toHaveLength(1));
    expect(b.called("PUT", `/pipelines/${p.id}/draft`)[0].body).toMatchObject({ spec: { tasks: [{ key: "hood", paramOverrides: { symbol: "HOOD" } }] } });
    // The task itself is never touched by a node's override.
    expect(b.called("PUT", "/tasks")).toHaveLength(0);
    expect(b.called("POST", "/tasks")).toHaveLength(0);
  });

  it("refuses anything but a JSON object, without changing the node", async () => {
    const { p } = quotes();
    renderApp(b, { path: path(p.id) });
    await clickNode("hood");
    await screen.findByRole("region", { name: "Parameters for this node" });
    setOverrides('["HOOD"]');
    expect(await screen.findByText(/must be a JSON object/)).toBeInTheDocument();
    setOverrides("{not json");
    expect(screen.getByLabelText("This node's overrides (JSON)")).toHaveAttribute("aria-invalid", "true");
    expect(within(toolbar()).queryByText("Unsaved changes")).not.toBeInTheDocument();
    expect(screen.queryByTestId("task-node-overrides-hood")).not.toBeInTheDocument();
  });

  it("Duplicate node copies the task, version and overrides under a new key, for another value", async () => {
    const { p, hood } = quotes();
    b.savePipelineDraft(p.id, [{ ...hood, paramOverrides: { symbol: "HOOD" } }]);
    renderApp(b, { path: path(p.id) });
    await clickNode("hood");
    await userEvent.click(within(await screen.findByRole("region", { name: "Node hood" })).getByRole("button", { name: "Duplicate node" }));

    expect(await screen.findByRole("region", { name: "Node hood_1" })).toBeInTheDocument(); // the copy is selected
    expect(screen.getByTestId("task-node-overrides-hood_1")).toHaveTextContent("symbol=HOOD");
    setOverrides('{"symbol": "TSLQ"}');
    expect(screen.getByTestId("task-node-overrides-hood_1")).toHaveTextContent("symbol=TSLQ");
    expect(screen.getByTestId("task-node-overrides-hood")).toHaveTextContent("symbol=HOOD"); // the original is untouched

    await userEvent.click(within(toolbar()).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(b.called("PUT", `/pipelines/${p.id}/draft`)).toHaveLength(1));
    const saved = (b.called("PUT", `/pipelines/${p.id}/draft`)[0].body as { spec: { tasks: TaskRef[] } }).spec.tasks;
    expect(saved.map((t) => [t.key, t.taskId, t.paramOverrides])).toEqual([
      ["hood", hood.taskId, { symbol: "HOOD" }],
      ["hood_1", hood.taskId, { symbol: "TSLQ" }], // same task, its own value
    ]);
  });

  it("routes a server error about a node's overrides to that field", async () => {
    const { p } = quotes();
    b.failNext.set(`PUT /pipelines/${p.id}/draft`, { status: 422, error: "params with this node's overrides are limited to 65536 bytes", field: "tasks[0].paramOverrides" });
    renderApp(b, { path: path(p.id) });
    await clickNode("hood");
    await screen.findByRole("region", { name: "Parameters for this node" });
    setOverrides('{"symbol": "HOOD"}');
    await userEvent.click(within(toolbar()).getByRole("button", { name: "Save" }));
    expect(await screen.findByLabelText("This node's overrides (JSON)")).toHaveAccessibleDescription(/limited to 65536 bytes/);
  });

  it("shows a viewer the node's overrides and the params it runs with, read-only", async () => {
    const { p, hood } = quotes();
    b.saveVersion(p.id, [{ ...hood, paramOverrides: { symbol: "HOOD" } }]);
    renderApp(b, { path: path(p.id), role: "viewer" });
    await clickNode("hood");
    const details = await screen.findByRole("region", { name: /Task hood details/ });
    await waitFor(() => expect(within(details).getAllByText(/"symbol": "HOOD"/).length).toBeGreaterThanOrEqual(2)); // overrides + effective params
    expect(screen.queryByLabelText("This node's overrides (JSON)")).not.toBeInTheDocument();
  });
});

describe("override helpers", () => {
  it("merges exactly as the server does: shallow, key by key", () => {
    expect(effectiveParams({ symbol: "BASE", w: { d: 5, u: "d" }, keep: 1 }, { symbol: "HOOD", w: { d: 1 } })).toEqual({ symbol: "HOOD", w: { d: 1 }, keep: 1 });
    expect(effectiveParams({ a: 1 }, undefined)).toEqual({ a: 1 });
  });

  it("labels a node by its first override", () => {
    expect(overridesLabel(undefined)).toBeNull();
    expect(overridesLabel({})).toBeNull();
    expect(overridesLabel({ symbol: "HOOD" })).toBe("symbol=HOOD");
    expect(overridesLabel({ symbol: "HOOD", n: 3, x: true })).toBe("symbol=HOOD +2");
    expect(overridesLabel({ n: 3 })).toBe("n=3");
  });

  it("duplicateRef copies wiring and overrides without sharing the objects", () => {
    const refs: TaskRef[] = [
      { key: "up", taskId: "t0", taskVersion: 1, dependsOn: [], position: { x: 0, y: 0 } },
      { key: "hood", taskId: "t1", taskVersion: "latest", dependsOn: ["up"], position: { x: 300, y: 0 }, paramOverrides: { symbol: "HOOD" } },
    ];
    const out = duplicateRef(refs, "hood");
    expect(out.key).toBe("hood_1");
    const copy = out.refs.find((r) => r.key === "hood_1")!;
    expect(copy).toMatchObject({ taskId: "t1", taskVersion: "latest", dependsOn: ["up"], paramOverrides: { symbol: "HOOD" } });
    expect(copy.position.y).toBeGreaterThan(0);
    expect(copy.paramOverrides).not.toBe(refs[1].paramOverrides);
    expect(copy.dependsOn).not.toBe(refs[1].dependsOn);
    expect(duplicateRef(refs, "missing")).toEqual({ refs, key: null });
  });
});
