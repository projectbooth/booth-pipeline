import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PipelineApp } from "../PipelineApp";
import type { RunDetail, WorkspaceRole } from "../types";
import { ETL, FakeBackend, getToken } from "./testUtils";

vi.mock("../components/DagCanvas", () => ({
  DagCanvas: (props: { tasks: { key: string }[]; statuses?: Record<string, string>; onSelect: (k: string | null) => void }) => (
    <div data-testid="canvas">
      {props.tasks.map((t) => (
        <button key={t.key} type="button" data-testid={`node-${t.key}`} onClick={() => props.onSelect(t.key)}>
          {t.key} [{props.statuses?.[t.key] ?? "-"}]
        </button>
      ))}
    </div>
  ),
}));

let be: FakeBackend;
let run: RunDetail;

const at = "2026-09-21T12:00:00+00:00";
const tr = (taskKey: string, status: RunDetail["tasks"][number]["status"], extra: Partial<RunDetail["tasks"][number]> = {}) => ({
  taskKey,
  status,
  attempts: status === "pending" ? 0 : 1,
  startedAt: status === "pending" ? null : at,
  finishedAt: ["succeeded", "failed"].includes(status) ? at : null,
  error: null,
  ...extra,
});

beforeEach(() => {
  be = new FakeBackend();
  be.install();
  be.addPipeline("etl", ETL());
  be.jobs.push({ id: "j1", name: "nightly", pipelineId: "p1", pipelineVersion: null, schedule: null, retry: null, allowConcurrentRuns: false, roleCeiling: "editor", hasOwner: true, createdBy: "e", createdAt: at, updatedAt: at, nextRunAt: null });
  run = be.newRun("j1");
  run.tasks = [tr("extract", "succeeded"), tr("clean", "running"), tr("load", "pending")];
  be.logs.set(run.id, [
    { seq: 1, taskKey: null, stream: "system", message: "run started" },
    { seq: 2, taskKey: "extract", stream: "stdout", message: "extracting" },
  ]);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mount(role: WorkspaceRole = "editor") {
  window.history.replaceState({}, "", `/pipeline/runs/${run.id}`);
  return render(<PipelineApp workspace="acme" role={role} theme="light" getAccessToken={getToken} />);
}

const logBox = () => screen.getByRole("log");

describe("run view", () => {
  it("shows the DAG with each task's live status, a task table, and the run's logs", async () => {
    mount();
    expect(await screen.findByTestId("node-extract")).toHaveTextContent("[succeeded]");
    expect(screen.getByTestId("node-clean")).toHaveTextContent("[running]");
    expect(screen.getByTestId("node-load")).toHaveTextContent("[pending]");
    const table = screen.getByRole("region", { name: "Tasks" });
    expect(within(table).getAllByRole("row")).toHaveLength(4); // header + 3 tasks
    await waitFor(() => expect(logBox()).toHaveTextContent("extracting"));
    expect(logBox()).toHaveTextContent("run started");
    expect(screen.getByText("Logs — whole run")).toBeInTheDocument();
  });

  it("polls with the seq cursor, appends only what is new, and stops once the server says done", async () => {
    mount();
    await waitFor(() => expect(logBox()).toHaveTextContent("extracting"));
    const first = be.called("GET", `/runs/${run.id}/logs`);
    expect(first[0].path).toContain("after=0"); // the first poll starts from the top
    // more output arrives, then the run ends
    be.logs.get(run.id)!.push({ seq: 3, taskKey: "clean", stream: "stdout", message: "cleaning rows" });
    await waitFor(() => expect(logBox()).toHaveTextContent("cleaning rows"), { timeout: 4000 });
    const polled = be.called("GET", `/runs/${run.id}/logs`);
    expect(polled.some((c) => c.path.includes("after=2"))).toBe(true); // asked only for what followed seq 2
    expect(logBox().textContent!.match(/extracting/g)).toHaveLength(1); // nothing was fetched twice

    run.status = "succeeded";
    run.finishedAt = at;
    run.tasks = [tr("extract", "succeeded"), tr("clean", "succeeded"), tr("load", "succeeded")];
    be.logs.get(run.id)!.push({ seq: 4, taskKey: null, stream: "system", message: "run succeeded after 3.0s" });
    await waitFor(() => expect(logBox()).toHaveTextContent("run succeeded after 3.0s"), { timeout: 4000 });
    await waitFor(() => expect(screen.getAllByText("succeeded").length).toBeGreaterThan(1), { timeout: 4000 });

    // done: no further log polling
    await new Promise((r) => setTimeout(r, 200));
    const settled = be.called("GET", `/runs/${run.id}/logs`).length;
    await new Promise((r) => setTimeout(r, 3200));
    expect(be.called("GET", `/runs/${run.id}/logs`).length).toBe(settled);
  }, 20000);

  it("clicking a task shows only that task's log; 'Show the whole run' returns", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(logBox()).toHaveTextContent("extracting"));
    await user.click(screen.getByTestId("node-extract"));
    expect(await screen.findByText("Logs — extract")).toBeInTheDocument();
    await waitFor(() => expect(be.called("GET", `/runs/${run.id}/logs`).some((c) => c.path.includes("task=extract"))).toBe(true));
    await waitFor(() => expect(logBox()).toHaveTextContent("extracting"));
    expect(logBox()).not.toHaveTextContent("run started"); // run-level lines are not part of a task's log
    await user.click(screen.getByRole("button", { name: "Show the whole run" }));
    await waitFor(() => expect(logBox()).toHaveTextContent("run started"));
  });

  it("marks the attempt on retried output so a retry is readable", async () => {
    run.tasks = [tr("extract", "succeeded", { attempts: 2 }), tr("clean", "pending"), tr("load", "pending")];
    be.logs.get(run.id)!.push({ seq: 3, taskKey: "extract", stream: "stderr", message: "flaky failure", attempt: 1 }, { seq: 4, taskKey: "extract", stream: "stdout", message: "worked", attempt: 2 });
    mount();
    await waitFor(() => expect(logBox()).toHaveTextContent("worked"));
    expect(within(logBox()).getByText("#2")).toBeInTheDocument();
    expect(within(logBox()).queryByText("#1")).not.toBeInTheDocument();
  });

  it("an editor can cancel an active run; a viewer cannot", async () => {
    const user = userEvent.setup();
    mount("editor");
    await user.click(await screen.findByRole("button", { name: "Cancel run" }));
    await waitFor(() => expect(be.called("POST", `/runs/${run.id}/cancel`)).toHaveLength(1));
    cleanup();
    run.cancelRequested = false;
    mount("viewer");
    await screen.findByTestId("node-extract");
    expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();
  });

  it("a failed run explains itself and there is nothing left to cancel", async () => {
    run.status = "failed";
    run.error = "task(s) failed: clean";
    run.tasks = [tr("extract", "succeeded"), tr("clean", "failed", { error: "task exited with code 1" }), tr("load", "skipped")];
    mount();
    expect(await screen.findByText("task(s) failed: clean")).toBeInTheDocument();
    expect(screen.getByText("task exited with code 1")).toBeInTheDocument();
    expect(screen.getByTestId("node-load")).toHaveTextContent("[skipped]");
    expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();
  });

  it("an unknown run is a clear error, not a blank page", async () => {
    window.history.replaceState({}, "", "/pipeline/runs/does-not-exist");
    render(<PipelineApp workspace="acme" role="editor" theme="light" getAccessToken={getToken} />);
    expect((await screen.findAllByText(/run not found/)).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: "Retry" }).length).toBeGreaterThan(0);
  });
});
