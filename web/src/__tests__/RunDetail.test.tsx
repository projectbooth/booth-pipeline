import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// S3 (docs/decisions/0016 §4).

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
});
afterEach(() => vi.unstubAllGlobals());

function seededRun(status: "running" | "failed" = "failed") {
  const p = b.addPipeline("churn", [b.ref("train"), { ...b.ref("evaluate", ["train"]), position: { x: 300, y: 0 } }]);
  const r = b.addRun(p.id, status, "2026-09-25T21:02:00+00:00", {
    trigger: "manual",
    triggeredBy: "bob",
    error: status === "failed" ? "task(s) failed: evaluate" : null,
    tasks: [
      { taskKey: "train", status: "succeeded", attempts: 1, startedAt: "2026-09-25T21:02:01+00:00", finishedAt: "2026-09-25T21:02:03+00:00", error: null },
      { taskKey: "evaluate", status: status === "failed" ? "failed" : "running", attempts: 1, startedAt: "2026-09-25T21:02:03+00:00", finishedAt: null, error: status === "failed" ? "exited with code 1" : null },
    ],
  });
  b.logs.set(r.id, [
    { seq: 1, taskKey: "train", stream: "stdout", message: "training" },
    { seq: 2, taskKey: "evaluate", stream: "stderr", message: "upstream file missing" },
  ]);
  return { p, r };
}

describe("Run detail", () => {
  it("shows the breadcrumb, status, error, stat strip, coloured DAG and per-node table", async () => {
    const { r } = seededRun();
    renderApp(b, { path: `/pipeline/runs/${r.id}` });
    expect(await screen.findByRole("heading", { name: `Run ${r.id.slice(0, 8)}` })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "churn" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("task(s) failed: evaluate");
    expect(within(screen.getByLabelText("Run summary")).getByText("2/2 done")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("task-node-evaluate")).toHaveAttribute("data-status", "failed"));
    expect(screen.getByTestId("task-node-train")).toHaveAttribute("data-status", "succeeded");
    const table = screen.getByRole("table", { name: "Tasks in this run" });
    expect(within(table).getByText("exited with code 1")).toBeInTheDocument();
  });

  it("filters logs to one node, from the table or from a ?task= link", async () => {
    const { r } = seededRun();
    renderApp(b, { path: `/pipeline/runs/${r.id}` });
    const log = await screen.findByRole("log", { name: "Logs for the whole run" });
    await waitFor(() => expect(log).toHaveTextContent("training"));
    await userEvent.click(screen.getByRole("button", { name: "Logs for evaluate" }));
    const one = await screen.findByRole("log", { name: "Logs for task evaluate" });
    await waitFor(() => expect(one).toHaveTextContent("upstream file missing"));
    expect(one).not.toHaveTextContent("training");
  });

  it("opens straight onto one node's logs from a ?task= link", async () => {
    const { r } = seededRun();
    renderApp(b, { path: `/pipeline/runs/${r.id}?task=evaluate` });
    expect(await screen.findByRole("log", { name: "Logs for task evaluate" })).toBeInTheDocument();
  });

  it("cancels an active run", async () => {
    const { r } = seededRun("running");
    renderApp(b, { path: `/pipeline/runs/${r.id}` });
    await userEvent.click(await screen.findByRole("button", { name: "Cancel run" }));
    expect(b.called("POST", `/runs/${r.id}/cancel`)).toHaveLength(1);
    expect(await screen.findByRole("button", { name: "Cancelling…" })).toBeDisabled();
  });

  it("offers viewers no cancel", async () => {
    const { r } = seededRun("running");
    renderApp(b, { path: `/pipeline/runs/${r.id}`, role: "viewer" });
    await screen.findByRole("heading", { name: `Run ${r.id.slice(0, 8)}` });
    expect(screen.queryByRole("button", { name: /Cancel/ })).not.toBeInTheDocument();
  });
});
