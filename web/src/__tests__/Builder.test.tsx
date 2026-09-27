import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// S2a editing, S2d editing, and the unsaved-work guard (docs/decisions/0016 §4, phase 3).

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
});
afterEach(() => vi.unstubAllGlobals());

function etl() {
  const extract = b.ref("extract");
  const load = { ...b.ref("load", ["extract"]), position: { x: 300, y: 0 } };
  const p = b.addPipeline("daily-etl", [extract, load]);
  return { p, extract, load };
}

const path = (id: string, tab = "") => `/pipeline/pipelines/${id}${tab ? `/${tab}` : ""}`;
// A plain click on a node: user-event's mousedown has no `view` in jsdom, which d3-zoom (under
// React Flow) dereferences. React Flow's onNodeClick only needs the click.
const clickNode = async (key: string) => fireEvent.click(await screen.findByTestId(`task-node-${key}`));
const toolbar = () => screen.getByRole("toolbar", { name: "Pipeline builder" });

describe("adding a task", () => {
  it("opens the picker BELOW the canvas and wires an existing task without creating one", async () => {
    const { p } = etl();
    const spare = b.addTask("spare", b.taskVersions.get(b.tasks[0].id)![0].config);
    b.addTask("orphan"); // unconfigured: hidden by default
    renderApp(b, { path: path(p.id) });
    await userEvent.click(await within(await screen.findByRole("toolbar", { name: "Pipeline builder" })).findByRole("button", { name: "+ Add task" }));

    const picker = screen.getByRole("region", { name: "Add a task" });
    const canvas = screen.getByRole("region", { name: "Pipeline DAG" });
    expect(canvas.compareDocumentPosition(picker) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // jsdom computes no Tailwind, so assert the fixed-size classes (0016 §6): 480px below 640px, else 560px.
    expect(canvas.className).toContain("h-[480px] sm:h-[560px]");

    const list = within(picker).getByRole("list", { name: "Matching tasks" });
    await within(list).findByText("spare");
    expect(within(list).queryByText("orphan")).not.toBeInTheDocument();
    await userEvent.click(within(picker).getByRole("button", { name: /Show 1 unconfigured task/ }));
    expect(within(list).getByText("orphan")).toBeInTheDocument();

    await userEvent.click(within(list).getByText("spare"));
    expect(await screen.findByTestId("task-node-task_1")).toBeInTheDocument();
    expect(b.called("POST", "/tasks")).toHaveLength(0);
    expect(within(toolbar()).getByText("Unsaved changes")).toBeInTheDocument();
    // The new node is selected, and its editor opens in the same place below the canvas.
    expect(await screen.findByRole("region", { name: "Node task_1" })).toBeInTheDocument();
    expect(b.tasks.find((t) => t.id === spare.id)).toBeDefined();
    expect(canvas.className).toContain("h-[480px] sm:h-[560px]");
  });

  it("creates a task only when asked to, by typing a new name", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id) });
    await userEvent.click(await within(await screen.findByRole("toolbar", { name: "Pipeline builder" })).findByRole("button", { name: "+ Add task" }));
    await userEvent.type(screen.getByRole("searchbox", { name: "Search tasks" }), "brand_new");
    await userEvent.click(await screen.findByRole("button", { name: '+ Create task "brand_new"' }));
    await screen.findByTestId("task-node-task_1");
    expect(b.called("POST", "/tasks")).toHaveLength(1);
    expect(b.called("POST", "/tasks")[0].body).toMatchObject({ name: "brand_new" });
  });
});

describe("saving the DAG", () => {
  it("plain Save writes the draft only, and says which version runs still use", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id) });
    await userEvent.click(await within(await screen.findByRole("toolbar", { name: "Pipeline builder" })).findByRole("button", { name: "Tidy layout" }));
    await userEvent.click(within(toolbar()).getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Saved\. Runs still use v1 until you save a new version/)).toBeInTheDocument();
    expect(b.called("PUT", `/pipelines/${p.id}/draft`)).toHaveLength(1);
    expect(b.called("POST", `/pipelines/${p.id}/versions`)).toHaveLength(0);
    expect(within(toolbar()).queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("Save as new version asks for an optional note inline, then snapshots a version", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id) });
    await userEvent.click(await within(await screen.findByRole("toolbar", { name: "Pipeline builder" })).findByRole("button", { name: "Tidy layout" }));
    await userEvent.click(within(toolbar()).getByRole("button", { name: "Save as new version…" }));
    const group = screen.getByRole("group", { name: "New pipeline version" });
    expect(group).toHaveTextContent("Save as v2");
    await userEvent.type(within(group).getByRole("textbox", { name: "Version note" }), "tidied");
    await userEvent.click(within(group).getByRole("button", { name: "Save version" }));
    expect(await screen.findByText(/Saved as v2\. The next run uses it\./)).toBeInTheDocument();
    expect(b.called("POST", `/pipelines/${p.id}/versions`)[0].body).toMatchObject({ notes: "tidied" });
  });

  it("routes a save refusal to the node and field it names", async () => {
    const { p } = etl();
    b.failNext.set(`POST /pipelines/${p.id}/versions`, { status: 422, error: "task 'load' has no version 9", field: "tasks[1].taskVersion" });
    renderApp(b, { path: path(p.id) });
    await userEvent.click(await within(await screen.findByRole("toolbar", { name: "Pipeline builder" })).findByRole("button", { name: "Tidy layout" }));
    await userEvent.click(within(toolbar()).getByRole("button", { name: "Save as new version…" }));
    await userEvent.click(screen.getByRole("button", { name: "Save version" }));
    const editor = await screen.findByRole("region", { name: "Node load" });
    expect(within(editor).getByLabelText("Version")).toHaveAccessibleDescription(/has no version 9/);
    expect(screen.getByTestId("task-node-load")).toHaveAccessibleName(/has an error/);
  });

  it("shows the server's live validation as a warning", async () => {
    const { p } = etl();
    b.validation = { valid: false, error: "a cycle", field: "tasks[0].dependsOn" };
    renderApp(b, { path: path(p.id) });
    expect(await screen.findByText("Not ready to save as a version: a cycle", {}, { timeout: 2000 })).toBeInTheDocument();
  });

  it("discards after an inline confirm", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id) });
    await userEvent.click(await within(await screen.findByRole("toolbar", { name: "Pipeline builder" })).findByRole("button", { name: "Tidy layout" }));
    await userEvent.click(within(toolbar()).getByRole("button", { name: "Discard" }));
    await userEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(within(toolbar()).queryByText("Unsaved changes")).not.toBeInTheDocument();
  });
});

describe("unsaved work", () => {
  it("survives switching tabs within the pipeline", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id) });
    await userEvent.click(await within(await screen.findByRole("toolbar", { name: "Pipeline builder" })).findByRole("button", { name: "Tidy layout" }));
    const tabs = screen.getByRole("navigation", { name: "Pipeline views" });
    await userEvent.click(within(tabs).getByRole("link", { name: "Runs" }));
    expect(window.location.pathname).toBe(path(p.id, "runs"));
    await userEvent.click(within(tabs).getByRole("link", { name: "DAG" }));
    expect(within(toolbar()).getByText("Unsaved changes")).toBeInTheDocument();
  });

  it("asks before leaving the pipeline with unsaved work", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id) });
    await userEvent.click(await within(await screen.findByRole("toolbar", { name: "Pipeline builder" })).findByRole("button", { name: "Tidy layout" }));
    await userEvent.click(screen.getByRole("link", { name: "← Pipelines" }));
    expect(window.location.pathname).toBe(path(p.id));
    expect(screen.getByText(/unsaved changes to this pipeline's DAG/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(window.location.pathname).toBe(path(p.id));
    await userEvent.click(screen.getByRole("link", { name: "← Pipelines" }));
    await userEvent.click(screen.getByRole("button", { name: "Discard and leave" }));
    expect(window.location.pathname).toBe("/pipeline/pipelines");
  });

  it("Run now with unsaved edits starts the run without navigating away from them", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id) });
    await userEvent.click(await within(await screen.findByRole("toolbar", { name: "Pipeline builder" })).findByRole("button", { name: "Tidy layout" }));
    expect(screen.getByText(/your unsaved and draft changes aren't in it/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Run now/ }));
    expect(await screen.findByText(/Started run/)).toBeInTheDocument();
    expect(window.location.pathname).toBe(path(p.id));
  });
});

describe("the node editor", () => {
  it("offers version pins with their creation times, and renames and rewires the node", async () => {
    const { p, load } = etl();
    b.saveTaskVersion(load.taskId, b.taskVersions.get(load.taskId)![0].config, "second");
    renderApp(b, { path: path(p.id) });
    await clickNode("load");
    const editor = await screen.findByRole("region", { name: "Node load" });
    const version = within(editor).getByLabelText("Version");
    await waitFor(() => expect(within(version).getByRole("option", { name: /^Pin to v2 — \d{4}-\d{2}-\d{2} \d{2}:\d{2} — second$/ })).toBeInTheDocument());

    const key = within(editor).getByLabelText(/^Key/);
    await userEvent.clear(key);
    await userEvent.type(key, "load_2{Enter}");
    expect(await screen.findByTestId("task-node-load_2")).toBeInTheDocument();
    expect(within(toolbar()).getByText("Unsaved changes")).toBeInTheDocument();
  });

  it("makes a pinned node's task settings read-only and says why", async () => {
    const { p } = etl(); // refs from b.ref() are pinned to v1
    renderApp(b, { path: path(p.id) });
    await clickNode("extract");
    const settings = await screen.findByRole("region", { name: "Task settings" });
    expect(within(settings).getByText(/pinned versions are frozen/)).toBeInTheDocument();
    expect(within(settings).queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
  });

  it("saves a 'latest' node's task with plain Save (draft) or as a new task version", async () => {
    const { p, extract } = etl();
    b.savePipelineDraft(p.id, [{ ...extract, taskVersion: "latest" }]);
    renderApp(b, { path: path(p.id) });
    await clickNode("extract");
    const settings = await screen.findByRole("region", { name: "Task settings" });
    const timeout = await within(settings).findByLabelText("Timeout (seconds)");
    await userEvent.clear(timeout);
    await userEvent.type(timeout, "120");
    await userEvent.click(within(settings).getByRole("button", { name: "Save" }));
    expect(await within(settings).findByText(/Everything following "latest" uses this now/)).toBeInTheDocument();
    expect(b.called("PUT", `/tasks/${extract.taskId}/draft`)[0].body).toMatchObject({ config: { timeoutSeconds: 120 } });

    await userEvent.clear(timeout);
    await userEvent.type(timeout, "90");
    await userEvent.click(within(settings).getByRole("button", { name: "Save as new version…" }));
    await userEvent.type(within(settings).getByRole("textbox", { name: "Task version note" }), "faster");
    await userEvent.click(within(settings).getByRole("button", { name: "Save version" }));
    expect(await within(settings).findByText(/Saved as task v2/)).toBeInTheDocument();
    expect(b.called("POST", `/tasks/${extract.taskId}/versions`)[0].body).toMatchObject({ notes: "faster", config: { timeoutSeconds: 90 } });
  });

  it("removes a node from the pipeline", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id) });
    await clickNode("load");
    await userEvent.click(within(await screen.findByRole("region", { name: "Node load" })).getByRole("button", { name: "Remove from pipeline" }));
    await waitFor(() => expect(screen.queryByTestId("task-node-load")).not.toBeInTheDocument());
  });
});

describe("the schedule editor", () => {
  it("edits in place, including a version pin with its creation time", async () => {
    const { p, extract } = etl();
    b.saveVersion(p.id, [extract], "trimmed");
    renderApp(b, { path: path(p.id, "schedule") });
    await userEvent.click(await screen.findByRole("button", { name: "Edit schedule" }));
    const form = screen.getByRole("form", { name: "Pipeline schedule" });
    await userEvent.click(within(form).getByLabelText("Run on a schedule"));
    await userEvent.selectOptions(within(form).getByLabelText("Version"), within(form).getByRole("option", { name: /^Pin to v1 — \d{4}-/ }));
    await userEvent.click(within(form).getByRole("button", { name: "Save schedule" }));
    expect(await screen.findByText("Schedule saved.")).toBeInTheDocument();
    expect(b.called("PUT", `/pipelines/${p.id}/schedule`)[0].body).toMatchObject({ pinnedVersion: 1, schedule: { type: "cron", enabled: true } });
    expect(screen.getByRole("region", { name: "Trigger" })).toHaveTextContent(/Pinned to v1/);
  });

  it("gives viewers no edit button", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id, "schedule"), role: "viewer" });
    await screen.findByRole("region", { name: "Trigger" });
    expect(screen.queryByRole("button", { name: "Edit schedule" })).not.toBeInTheDocument();
  });
});

describe("viewers", () => {
  it("get the canvas with no toolbar", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id), role: "viewer" });
    await screen.findByTestId("task-node-load");
    expect(screen.queryByRole("toolbar", { name: "Pipeline builder" })).not.toBeInTheDocument();
    expect(screen.getByText(/builder is view-only/)).toBeInTheDocument();
  });
});
