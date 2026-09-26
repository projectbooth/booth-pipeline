import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// S2 header + S2a (read side), S2c, S2d summary, S2e, S2f (docs/decisions/0016 §4).

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
});
afterEach(() => vi.unstubAllGlobals());

function etl() {
  const extract = b.ref("extract");
  const load = { ...b.ref("load", ["extract"]), position: { x: 300, y: 0 } };
  const p = b.addPipeline("daily-etl", [extract, load]);
  p.description = "moves data";
  p.schedule = { type: "cron", cron: "0 6 * * *", timezone: "UTC", enabled: true };
  p.nextRunAt = "2026-09-27T06:00:00+00:00";
  return { p, extract, load };
}

const path = (id: string, tab = "") => `/pipeline/pipelines/${id}${tab ? `/${tab}` : ""}`;

describe("pipeline header", () => {
  it("shows the stat strip: last run, duration, next run, schedule, owner, task count", async () => {
    const { p } = etl();
    b.addRun(p.id, "succeeded", "2026-09-24T06:00:00+00:00");
    renderApp(b, { path: path(p.id) });
    const strip = await screen.findByLabelText("Pipeline summary");
    expect(within(strip).getByText("Last run")).toBeInTheDocument();
    expect(within(strip).getByText("1m 05s")).toBeInTheDocument();
    expect(within(strip).getByText("0 6 * * * (UTC)")).toBeInTheDocument();
    expect(within(strip).getByText("eddie")).toBeInTheDocument();
    expect(within(strip).getByText("2")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "daily-etl" })).toBeInTheDocument();
    expect(screen.getByText("moves data")).toBeInTheDocument();
  });

  it("Run now is on every tab, says which version it runs, and opens the run", async () => {
    const { p } = etl();
    for (const tab of ["", "runs", "schedule", "config", "versions"]) {
      const { unmount } = renderApp(b, { path: path(p.id, tab) });
      expect(await screen.findByRole("button", { name: /Run now/ })).toBeEnabled();
      expect(screen.getByText("Runs v1 (latest saved version)")).toBeInTheDocument();
      unmount();
    }
    renderApp(b, { path: path(p.id, "config") });
    await userEvent.click(await screen.findByRole("button", { name: /Run now/ }));
    expect(b.called("POST", `/pipelines/${p.id}/run`)).toHaveLength(1);
    await waitFor(() => expect(window.location.pathname).toMatch(/^\/pipeline\/runs\//));
  });

  it("names a pinned version, and warns when draft changes won't be in the run", async () => {
    const { p, extract } = etl();
    b.saveVersion(p.id, [extract]); // v2
    p.pinnedVersion = 1;
    b.savePipelineDraft(p.id, [{ ...extract, position: { x: 99, y: 99 } }]); // draft ≠ v2
    renderApp(b, { path: path(p.id) });
    expect(await screen.findByText(/Runs v1 \(pinned\) — your draft changes aren't in it/)).toBeInTheDocument();
    expect(screen.getByText("Draft ahead of v2")).toBeInTheDocument();
  });

  it("can't run a pipeline with no saved version", async () => {
    const p = b.addPipeline("empty");
    renderApp(b, { path: path(p.id) });
    expect(await screen.findByRole("button", { name: /Run now/ })).toBeDisabled();
    expect(screen.getByText(/save a version first/)).toBeInTheDocument();
  });

  it("shows a refused run inline", async () => {
    const { p } = etl();
    b.failNext.set(`POST /pipelines/${p.id}/run`, { status: 409, error: "this pipeline already has an active run" });
    renderApp(b, { path: path(p.id) });
    await userEvent.click(await screen.findByRole("button", { name: /Run now/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already has an active run");
  });

  it("edits the pipeline's name and description", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id) });
    await userEvent.click(await screen.findByRole("button", { name: "More pipeline actions" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Edit details…" }));
    const name = screen.getByLabelText(/^Name/);
    await userEvent.clear(name);
    await userEvent.type(name, "nightly-etl");
    await userEvent.click(screen.getByRole("button", { name: "Save details" }));
    expect(await screen.findByRole("heading", { name: "nightly-etl" })).toBeInTheDocument();
    expect(b.called("PUT", `/pipelines/${p.id}`)[0].body).toEqual({ name: "nightly-etl", description: "moves data" });
  });

  it("deletes after an inline confirm and returns to the list", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id) });
    await userEvent.click(await screen.findByRole("button", { name: "More pipeline actions" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Delete pipeline…" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete pipeline" }));
    await waitFor(() => expect(window.location.pathname).toBe("/pipeline/pipelines"));
    expect(b.called("DELETE", `/pipelines/${p.id}`)).toHaveLength(1);
  });

  it("exports YAML for the version shown, from the menu", async () => {
    const { p } = etl();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    URL.createObjectURL = vi.fn(() => "blob:x");
    URL.revokeObjectURL = vi.fn();
    renderApp(b, { path: path(p.id) });
    await userEvent.click(await screen.findByRole("button", { name: "More pipeline actions" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Export v1 as YAML" }));
    await waitFor(() => expect(b.called("GET", `/pipelines/${p.id}/versions/1/export`)).toHaveLength(1));
    expect(click).toHaveBeenCalled();
    click.mockRestore();
  });

  it("gives viewers the page with no write controls", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id), role: "viewer" });
    await screen.findByRole("heading", { name: "daily-etl" });
    expect(screen.queryByRole("button", { name: /Run now/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "More pipeline actions" }));
    expect(screen.getByRole("menuitem", { name: /Export/ })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Delete/ })).not.toBeInTheDocument();
  });
});

describe("DAG tab (read side)", () => {
  it("labels nodes with their task names on load, not only after a click", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id) });
    const node = await screen.findByTestId("task-node-extract");
    await waitFor(() => expect(node).toHaveTextContent("extract"));
    expect(screen.getByTestId("task-node-load")).toHaveTextContent("PY");
  });

  it("colours nodes by the latest run, but not a node re-pointed at a different task since", async () => {
    const { p, extract, load } = etl();
    b.addRun(p.id, "running", "2026-09-26T06:00:00+00:00", {
      tasks: [
        { taskKey: "extract", status: "succeeded", attempts: 1, startedAt: null, finishedAt: null, error: null },
        { taskKey: "load", status: "running", attempts: 1, startedAt: null, finishedAt: null, error: null },
      ],
    });
    const other = b.addTask("other");
    b.savePipelineDraft(p.id, [extract, { ...load, taskId: other.id }]); // load now points elsewhere
    renderApp(b, { path: path(p.id) });
    await waitFor(() => expect(screen.getByTestId("task-node-extract")).toHaveAttribute("data-status", "succeeded"));
    expect(screen.getByTestId("task-node-load")).toHaveAttribute("data-status", "none");
    expect(screen.getByText(/Status from/)).toBeInTheDocument();
  });

  it("shows a node's details BELOW the canvas when it's selected", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id) });
    // A plain click: user-event's mousedown has no `view` in jsdom, which d3-zoom (under React
    // Flow) dereferences. React Flow's onNodeClick only needs the click itself.
    fireEvent.click(await screen.findByTestId("task-node-load"));
    const details = await screen.findByRole("region", { name: /Task load details/ });
    const canvas = screen.getByRole("region", { name: "Pipeline DAG" });
    // Document order: the canvas comes first, the panel after it — never beside or over it.
    expect(canvas.compareDocumentPosition(details) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(details).getByText("extract", { selector: "dd" })).toBeInTheDocument(); // depends on
    expect(within(details).getByText("base")).toBeInTheDocument(); // runner
  });

  it("opens an old version with a banner and version times in the picker", async () => {
    const { p, extract } = etl();
    b.saveVersion(p.id, [extract]); // v2 is latest
    renderApp(b, { path: `/pipeline/pipelines/${p.id}/v/1` });
    expect(await screen.findByText(/Viewing v1 of 2/)).toBeInTheDocument();
    const picker = screen.getByRole("combobox", { name: "Pipeline version shown" });
    expect(within(picker).getByRole("option", { name: /^v1 — \d{4}-\d{2}-\d{2} \d{2}:\d{2} — 2 tasks/ })).toBeInTheDocument();
    expect(await screen.findByTestId("task-node-load")).toBeInTheDocument();
  });
});

describe("Runs tab", () => {
  it("shows exact server counts, a history strip oldest→newest, and the runs table", async () => {
    const { p } = etl();
    b.addRun(p.id, "succeeded", "2026-09-20T06:00:00+00:00");
    b.addRun(p.id, "failed", "2026-09-21T06:00:00+00:00");
    b.addRun(p.id, "succeeded", "2026-09-22T06:00:00+00:00");
    b.addRun(p.id, "canceled", "2026-09-23T06:00:00+00:00");
    renderApp(b, { path: path(p.id, "runs") });
    expect(await screen.findByText("67%")).toBeInTheDocument(); // 2 of 3 finished; canceled left out
    expect(screen.getByText("of 3 finished")).toBeInTheDocument();
    const strip = screen.getByRole("list", { name: /Run history/ });
    const bars = within(strip).getAllByRole("link");
    expect(bars).toHaveLength(4);
    expect(bars[0]).toHaveAccessibleName(/succeeded, 2026-09-20/);
    expect(bars[3]).toHaveAccessibleName(/canceled/);
    expect(b.calls.some((c) => c.path.includes("status=failed"))).toBe(true);
  });

  it("has an empty state", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id, "runs") });
    expect(await screen.findByText("No runs yet")).toBeInTheDocument();
  });
});

describe("Schedule, Configuration and Versions tabs", () => {
  it("summarises the trigger read-only, including the version pin with its creation time", async () => {
    const { p } = etl();
    p.pinnedVersion = 1;
    renderApp(b, { path: path(p.id, "schedule") });
    const card = await screen.findByRole("region", { name: "Trigger" });
    expect(within(card).getByText(/daily at|cron 0 6/)).toBeInTheDocument();
    expect(within(card).getByText(/^Pinned to v1 — \d{4}-\d{2}-\d{2}/)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("tells a manual-only pipeline to use Run now", async () => {
    const p = b.addPipeline("manual", [b.ref("x")]);
    renderApp(b, { path: path(p.id, "schedule") });
    expect(await screen.findByText(/presses Run now, above/)).toBeInTheDocument();
  });

  it("shows flat, read-only configuration including each node's task", async () => {
    const { p } = etl();
    renderApp(b, { path: path(p.id, "config") });
    expect(await screen.findByText(p.id)).toBeInTheDocument();
    const tasks = screen.getByRole("region", { name: /Tasks/ });
    await waitFor(() => expect(within(tasks).getByRole("link", { name: "load" })).toBeInTheDocument());
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("lists versions with times, a draft row only when it differs, and per-version export", async () => {
    const { p, extract } = etl();
    b.saveVersion(p.id, [extract], "trimmed");
    renderApp(b, { path: path(p.id, "versions") });
    expect(await screen.findByText("trimmed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export v1 as YAML" })).toBeInTheDocument();
    expect(screen.queryByText("Draft")).not.toBeInTheDocument(); // draft == v2
    await userEvent.click(screen.getAllByRole("link", { name: "Open" })[1]);
    expect(window.location.pathname).toBe(`/pipeline/pipelines/${p.id}/v/1`);
  });
});
