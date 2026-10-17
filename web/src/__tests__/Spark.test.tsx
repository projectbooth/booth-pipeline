import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TaskConfig } from "../types";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// ADR 0119: a task that runs on booth-spark, in the task settings and in the run view.

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
});
afterEach(() => vi.unstubAllGlobals());

const cfg = (over: Partial<TaskConfig> = {}): TaskConfig => ({
  code: { type: "storage", backendId: "b1", path: "jobs/agg.py", language: "python", source: "def run(ctx):\n    return 1\n" },
  runner: "base",
  retry: null,
  params: {},
  timeoutSeconds: 3600,
  platformAccess: false,
  ...over,
});

async function editCard(id: string) {
  renderApp(b, { path: `/pipeline/tasks/${id}` });
  await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
  return screen.getByRole("region", { name: "Edit task settings" });
}

describe("a Spark task in the task settings", () => {
  it("choosing Spark shows its options, locks platform access on, hides packages, and saves the options", async () => {
    const t = b.addTask("agg", cfg());
    const card = await editCard(t.id);
    await userEvent.selectOptions(within(card).getByLabelText("Runs on"), "spark");
    const panel = within(card).getByRole("region", { name: "Spark options" });
    expect(panel).toHaveTextContent("booth-spark allows up to 2 executors, 4g per JVM, 2h per run.");
    const access = within(card).getByRole("checkbox", { name: /Platform access/ });
    expect(access).toBeChecked();
    expect(access).toBeDisabled();
    expect(within(card).queryByLabelText("Python packages")).not.toBeInTheDocument();
    expect(within(card).getByText(/waits at least 2.5 minutes/)).toBeInTheDocument();

    await userEvent.type(within(panel).getByLabelText("Driver memory"), "1g");
    await userEvent.type(within(panel).getByLabelText("Executors (max)"), "2");
    await userEvent.type(within(panel).getByLabelText("Spark settings"), "spark.sql.shuffle.partitions=8");
    await userEvent.type(within(panel).getByLabelText("Arguments"), "--season{enter}2026");
    await userEvent.click(within(panel).getByRole("checkbox", { name: /lakehouse/ }));
    await userEvent.click(within(panel).getByRole("button", { name: "+ Storage location" }));
    const loc = within(panel).getByRole("group", { name: "Storage location 1" });
    await userEvent.type(within(loc).getByLabelText("Storage backend"), "seaweedfs-spark");
    await userEvent.type(within(loc).getByLabelText("Path"), "nfl/raw");
    await userEvent.selectOptions(within(loc).getByLabelText("Access"), "readwrite");
    await userEvent.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(b.called("PUT", `/tasks/${t.id}/draft`)).toHaveLength(1));
    expect(b.called("PUT", `/tasks/${t.id}/draft`)[0].body).toMatchObject({
      config: {
        runner: "spark",
        platformAccess: true,
        spark: {
          resources: { driver: { memory: "1g" }, executors: { max: 2 } },
          conf: { "spark.sql.shuffle.partitions": "8" },
          args: ["--season", "2026"],
          dataAccess: { lakehouse: true, storage: [{ backendId: "seaweedfs-spark", path: "nfl/raw", access: "readwrite" }] },
        },
      },
    });
  });

  it("switching back to the base runner drops the Spark options", async () => {
    const t = b.addTask("agg", cfg({ runner: "spark", platformAccess: true, spark: { args: ["x"] } }));
    const card = await editCard(t.id);
    await userEvent.selectOptions(within(card).getByLabelText("Runs on"), "base");
    expect(within(card).queryByRole("region", { name: "Spark options" })).not.toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(b.called("PUT", `/tasks/${t.id}/draft`)).toHaveLength(1));
    expect((b.called("PUT", `/tasks/${t.id}/draft`)[0].body as { config: TaskConfig }).config.spark).toBeUndefined();
  });

  it("shows the server's refusal of a Spark option in the Spark panel", async () => {
    const t = b.addTask("agg", cfg({ runner: "spark", platformAccess: true, spark: {} }));
    b.failNext.set(`PUT /tasks/${t.id}/draft`, { status: 422, error: "resources.executors.min is more than max", field: "spark.resources" });
    const card = await editCard(t.id);
    const panel = within(card).getByRole("region", { name: "Spark options" });
    await userEvent.type(within(panel).getByLabelText("Executors (min)"), "3"); // something to save
    await userEvent.click(within(card).getByRole("button", { name: "Save" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("resources.executors.min is more than max");
  });
});

describe("a Spark task in the run view", () => {
  it("shows its application's state and links to it, and its driver log lines", async () => {
    const p = b.addPipeline("nfl", [{ key: "agg", taskId: "t1", taskVersion: 1, dependsOn: [], position: { x: 0, y: 0 }, paramOverrides: {} }]);
    const r = b.newRun(p.id);
    r.status = "running";
    r.tasks = [{ taskKey: "agg", status: "running", attempts: 1, startedAt: null, finishedAt: null, error: null, external: { kind: "spark", id: "7f3c2a9be1d0", state: "running", reason: null } }];
    b.logs.set(r.id, [
      { seq: 1, taskKey: "agg", stream: "system", message: "spark: submitted application 7f3c2a9be1d0" },
      { seq: 2, taskKey: "agg", stream: "spark", message: "counted 21 rows" },
    ]);
    renderApp(b, { path: `/pipeline/runs/${r.id}` });
    const chip = await screen.findByRole("link", { name: "Spark · running · 7f3c2a9b" });
    expect(chip).toHaveAttribute("href", "/spark/runs/7f3c2a9be1d0");
    await userEvent.click(screen.getByRole("button", { name: "Logs for agg" }));
    expect(await screen.findByText("counted 21 rows")).toBeInTheDocument();
  });
});
