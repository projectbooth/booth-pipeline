import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TaskConfig } from "../types";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// A task's choice of an operator-built runner image (the chart's runner.images).

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
});
afterEach(() => vi.unstubAllGlobals());

const cfg = (over: Partial<TaskConfig> = {}): TaskConfig => ({
  code: { type: "storage", backendId: "b1", path: "jobs/capture.py", language: "python", source: "import yfinance\n" },
  runner: "base",
  retry: null,
  params: {},
  timeoutSeconds: 600,
  platformAccess: false,
  ...over,
});

async function editCard(id: string) {
  renderApp(b, { path: `/pipeline/tasks/${id}` });
  await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
  return screen.getByRole("region", { name: "Edit task settings" });
}

describe("Runner image", () => {
  it("chooses one of the install's images and saves it with the task", async () => {
    b.runnerImages = ["stocks-py"];
    const t = b.addTask("capture", cfg());
    const card = await editCard(t.id);
    const select = await within(card).findByLabelText("Runner image");
    expect(select).toHaveValue("");
    await userEvent.selectOptions(select, "stocks-py");
    await userEvent.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(b.called("PUT", `/tasks/${t.id}/draft`)).toHaveLength(1));
    expect(b.called("PUT", `/tasks/${t.id}/draft`)[0].body).toMatchObject({ config: { runnerImage: "stocks-py" } });
  });

  it("isn't shown when the install has none", async () => {
    const t = b.addTask("capture", cfg());
    const card = await editCard(t.id);
    await waitFor(() => expect(b.called("GET", "/config").length).toBeGreaterThan(0));
    expect(within(card).queryByLabelText("Runner image")).not.toBeInTheDocument();
  });

  it("keeps showing an image the install no longer has, marked so", async () => {
    const t = b.addTask("capture", cfg({ runnerImage: "old-py" }));
    const card = await editCard(t.id);
    const select = await within(card).findByLabelText("Runner image");
    expect(select).toHaveValue("old-py");
    expect(within(select).getByRole("option", { name: "old-py (no longer configured)" })).toBeInTheDocument();
  });
});
