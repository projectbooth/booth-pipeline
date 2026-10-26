import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { importName } from "../task/ModulesField";
import type { TaskConfig } from "../types";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// Shared modules from the code catalog, chosen in a task's settings and pinned by the server at save.

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
});
afterEach(() => vi.unstubAllGlobals());

const cfg = (over: Partial<TaskConfig> = {}): TaskConfig => ({
  code: { type: "storage", backendId: "b1", path: "jobs/capture.py", language: "python", source: "import loader\n" },
  runner: "base",
  retry: null,
  params: {},
  timeoutSeconds: 600,
  platformAccess: false,
  ...over,
});

async function modulesSection(id: string) {
  renderApp(b, { path: `/pipeline/tasks/${id}` });
  await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
  const card = screen.getByRole("region", { name: "Edit task settings" });
  return { card, section: within(card).getByRole("region", { name: "Shared modules" }) };
}

describe("Shared modules", () => {
  it("derives an import name from a catalog entry's name", () => {
    expect(importName("Market calendar")).toBe("market_calendar");
    expect(importName("loader.py")).toBe("loader");
    expect(importName("2fa-helpers")).toBe("m_2fa_helpers");
  });

  it("imports a catalog entry, at latest until saved, and saves the reference", async () => {
    const t = b.addTask("capture", cfg());
    const { card, section } = await modulesSection(t.id);
    await userEvent.click(within(section).getByRole("button", { name: "Add a module" }));
    await userEvent.click(await within(section).findByRole("button", { name: "Import loader" }));
    expect(within(section).getByLabelText("Import name for loader")).toHaveValue("loader");
    expect(within(section).getByLabelText("Version of loader")).toHaveValue("latest");
    await userEvent.selectOptions(within(section).getByLabelText("Version of loader"), "1.0.0");
    await userEvent.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(b.called("PUT", `/tasks/${t.id}/draft`)).toHaveLength(1));
    expect(b.called("PUT", `/tasks/${t.id}/draft`)[0].body).toMatchObject({ config: { modules: [{ name: "loader", entryId: "e1", version: "1.0.0" }] } });
  });

  it("shows a saved module's snapshot, and drops it when another version is chosen", async () => {
    const t = b.addTask("capture", cfg({ modules: [{ name: "loader", entryId: "e1", version: "1.0.0", entryName: "loader", sha256: "abcdef0123456789", source: "x = 1\n" }] }));
    const { card, section } = await modulesSection(t.id);
    expect(within(section).getByText(/Saved snapshot/)).toHaveTextContent("abcdef012345");
    await userEvent.selectOptions(within(section).getByLabelText("Version of loader"), "2.0.0");
    expect(within(section).queryByText(/Saved snapshot/)).not.toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(b.called("PUT", `/tasks/${t.id}/draft`)).toHaveLength(1));
    const sent = (b.called("PUT", `/tasks/${t.id}/draft`)[0].body as { config: TaskConfig }).config.modules![0];
    expect(sent).toEqual({ name: "loader", entryId: "e1", version: "2.0.0", entryName: "loader" }); // the server fetches it afresh
  });

  it("shows the server's refusal on the module's row", async () => {
    const t = b.addTask("capture", cfg({ modules: [{ name: "json", entryId: "e1", version: "1.0.0", entryName: "loader" }] }));
    b.failNext.set(`PUT /tasks/${t.id}/draft`, { status: 422, error: "'json' would shadow a module the task or Python itself needs; choose another name", field: "modules[0].name" });
    const { card, section } = await modulesSection(t.id);
    await userEvent.type(within(section).getByLabelText("Import name for loader"), "x"); // jsonx: an edit, so Save sends
    await userEvent.click(within(card).getByRole("button", { name: "Save" }));
    expect(await within(section).findByText(/would shadow a module/)).toBeInTheDocument();
    expect(within(card).getAllByText(/would shadow a module/)).toHaveLength(1);
  });
});
