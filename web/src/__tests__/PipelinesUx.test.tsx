import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { groupByPrefix, namePrefix, NO_PREFIX } from "../views/PipelineList";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// The user's pipelines-area feedback (2026-10-24): saves you can see, a compact canvas, and the list
// grouped by name until projects (ADR 0124) arrive.

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
  localStorage.clear();
});
afterEach(() => vi.unstubAllGlobals());

const path = (id: string, tab = "") => `/pipeline/pipelines/${id}${tab ? `/${tab}` : ""}`;

function scheduled() {
  const p = b.addPipeline("daily-etl", [b.ref("extract")]);
  p.schedule = { type: "cron", cron: "0 6 * * *", timezone: "UTC", enabled: true };
  p.hasOwner = true;
  p.owner = { kind: "person", name: "eddie", you: true };
  return p;
}

describe("saving the schedule", () => {
  it("puts Save and the unsaved state above the form, and saves only a change", async () => {
    const p = scheduled();
    renderApp(b, { path: path(p.id, "schedule") });
    await userEvent.click(await screen.findByRole("button", { name: "Edit schedule" }));
    const form = screen.getByRole("form", { name: "Pipeline schedule" });
    const actions = within(form).getByRole("group", { name: "Schedule actions" });
    const firstCard = within(form).getAllByRole("region")[0];
    expect(actions.compareDocumentPosition(firstCard) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const save = within(actions).getByRole("button", { name: "Save schedule" });
    expect(save).toBeDisabled();
    expect(actions).toHaveTextContent("No changes yet");
    await userEvent.click(within(form).getByLabelText(/Allow overlapping runs/));
    expect(within(actions).getByText("Unsaved changes")).toBeInTheDocument();
    expect(within(actions).getByRole("button", { name: "Discard changes" })).toBeInTheDocument();
    await userEvent.click(save);
    expect(await screen.findByText("Schedule saved.")).toBeInTheDocument();
    expect(b.called("PUT", `/pipelines/${p.id}/schedule`)).toHaveLength(1);
    expect(b.called("PUT", `/pipelines/${p.id}/schedule`)[0].body).toMatchObject({ allowConcurrentRuns: true, schedule: { cron: "0 6 * * *", enabled: true } });
  });

  it("asks before another tab drops an unsaved schedule", async () => {
    const p = scheduled();
    renderApp(b, { path: path(p.id, "schedule") });
    await userEvent.click(await screen.findByRole("button", { name: "Edit schedule" }));
    await userEvent.click(within(screen.getByRole("form", { name: "Pipeline schedule" })).getByLabelText(/Allow overlapping runs/));
    const tabs = screen.getByRole("navigation", { name: "Pipeline views" });
    await userEvent.click(within(tabs).getByRole("link", { name: "Runs" }));
    expect(window.location.pathname).toBe(path(p.id, "schedule"));
    expect(screen.getByText(/unsaved changes to the schedule/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("form", { name: "Pipeline schedule" })).toBeInTheDocument(); // still there, still changed
  });

  it("still lets a pipeline with no owner be saved unchanged: that is how it gets one", async () => {
    const p = scheduled();
    p.hasOwner = false;
    p.owner = null;
    renderApp(b, { path: path(p.id, "schedule") });
    await userEvent.click(await screen.findByRole("button", { name: "Edit schedule" }));
    expect(screen.getByRole("button", { name: "Save schedule" })).toBeEnabled();
  });
});

describe("the task editor in the DAG", () => {
  it("shows its save actions above the long form", async () => {
    const p = scheduled();
    b.savePipelineDraft(p.id, [{ ...b.versions.get(p.id)![0].spec.tasks[0], taskVersion: "latest" }]); // editable: follows the task's latest
    renderApp(b, { path: path(p.id) });
    fireEvent.click(await screen.findByTestId("task-node-extract"));
    const settings = await screen.findByRole("region", { name: "Task settings" });
    const timeout = await within(settings).findByLabelText("Timeout (seconds)");
    const save = within(settings).getByRole("button", { name: "Save as new version…" });
    expect(save.compareDocumentPosition(timeout) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(settings).getByText("No unsaved task changes")).toBeInTheDocument();
    await userEvent.clear(timeout);
    await userEvent.type(timeout, "120");
    expect(within(settings).getByText("Unsaved task changes")).toBeInTheDocument();
  });
});

describe("the canvas", () => {
  it("is compact by default and expands on request, remembered for next time", async () => {
    const p = scheduled();
    const view = renderApp(b, { path: path(p.id) });
    const canvas = await screen.findByRole("region", { name: "Pipeline DAG" });
    expect(canvas.className).toContain("h-[280px] sm:h-[320px]");
    await userEvent.click(screen.getByRole("button", { name: "Expand canvas" }));
    expect(canvas.className).toContain("h-[480px] sm:h-[560px]");
    expect(screen.getByRole("button", { name: "Shrink canvas" })).toHaveAttribute("aria-pressed", "true");
    view.unmount();
    renderApp(b, { path: path(p.id) });
    expect((await screen.findByRole("region", { name: "Pipeline DAG" })).className).toContain("h-[480px] sm:h-[560px]");
  });
});

describe("the run page's canvas", () => {
  it("is compact by default too, and expands on its own, remembered separately", async () => {
    const p = scheduled();
    const r = b.addRun(p.id, "succeeded", "2026-10-25T09:00:00+00:00", {
      tasks: [{ taskKey: "extract", status: "succeeded", attempts: 1, startedAt: null, finishedAt: null, error: null }],
    });
    renderApp(b, { path: `/pipeline/runs/${r.id}` });
    const canvas = await screen.findByRole("region", { name: "Run DAG" });
    expect(canvas.className).toContain("h-[280px] sm:h-[320px]");
    await userEvent.click(screen.getByRole("button", { name: "Expand canvas" }));
    expect(canvas.className).toContain("h-[420px]");
    expect(localStorage.getItem("booth-pipeline.run-dag-expanded")).toBe("1");
    expect(localStorage.getItem("booth-pipeline.dag-expanded")).toBeNull(); // the pipeline page's own choice is untouched
  });
});

describe("the pipeline list, grouped by name", () => {
  it("groups by the part before the first '-', with 'No prefix' last", () => {
    expect(namePrefix("finance-dev-silver")).toBe("finance");
    expect(namePrefix("NFL-catchup")).toBe("nfl");
    expect(namePrefix("silver")).toBe(NO_PREFIX);
    expect(namePrefix("-odd")).toBe(NO_PREFIX);
    const ps = ["nfl-catchup", "silver", "finance-prod", "finance-dev-silver"].map((name) => b.addPipeline(name));
    expect(groupByPrefix(ps).map((g) => [g.group, g.items.map((p) => p.name)])).toEqual([
      ["finance", ["finance-prod", "finance-dev-silver"]],
      ["nfl", ["nfl-catchup"]],
      [NO_PREFIX, ["silver"]],
    ]);
  });

  it("shows collapsible headings, says it groups by name, and can be turned off", async () => {
    for (const name of ["nfl-catchup", "finance-dev-silver", "finance-prod", "silver"]) b.addPipeline(name, [b.ref("x")]);
    renderApp(b, { path: "/pipeline/pipelines" });
    const finance = await screen.findByRole("button", { name: "finance" });
    expect(finance).toHaveAttribute("aria-expanded", "true");
    expect(finance.closest("td")).toHaveTextContent("2 pipelines");
    expect(screen.getByText("Grouped by name only; projects will replace this.")).toBeInTheDocument();
    await userEvent.click(finance);
    expect(finance).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: "finance-prod" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "nfl-catchup" })).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Group by"), "none");
    expect(screen.queryByRole("button", { name: "finance" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("link", { name: "finance-prod" })).toBeInTheDocument());
    expect(localStorage.getItem("booth-pipeline.list-group-by")).toBe("none");
  });
});
