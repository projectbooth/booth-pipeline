import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// S4 (docs/decisions/0016 §4).

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
});
afterEach(() => vi.unstubAllGlobals());

const row = (name: string) => screen.getByRole("link", { name }).closest("tr")!;

describe("Tasks list", () => {
  it("lists each task once, with where its versioning stands", async () => {
    const extract = b.ref("extract").taskId; // one saved version
    b.saveTaskVersion(extract, b.taskVersions.get(extract)![0].config); // now v2
    const draftOnly = b.addTask("sketch");
    b.saveTaskDraft(draftOnly.id, b.taskVersions.get(extract)![0].config);
    b.addTask("orphan");
    renderApp(b, { path: "/pipeline/tasks" });

    await screen.findByRole("link", { name: "extract" });
    expect(screen.getAllByRole("link", { name: "extract" })).toHaveLength(1);
    expect(within(row("extract")).getByText("v2")).toBeInTheDocument();
    expect(within(row("sketch")).getByText("Draft only")).toBeInTheDocument();
    expect(within(row("orphan")).getByText("Not configured yet")).toBeInTheDocument();
  });

  it("opens a task from its row", async () => {
    const t = b.addTask("extract");
    renderApp(b, { path: "/pipeline/tasks" });
    await userEvent.click(await screen.findByRole("link", { name: "extract" }));
    expect(window.location.pathname).toBe(`/pipeline/tasks/${t.id}`);
  });

  it("creates a task and opens it", async () => {
    renderApp(b, { path: "/pipeline/tasks" });
    await userEvent.click(await screen.findByRole("button", { name: "+ New task" }));
    await userEvent.type(screen.getByLabelText(/Name/), "load");
    await userEvent.type(screen.getByLabelText("Description"), "loads things");
    await userEvent.click(screen.getByRole("button", { name: "Create and configure" }));
    await waitFor(() => expect(window.location.pathname).toBe(`/pipeline/tasks/${b.tasks[0].id}`));
    expect(b.called("POST", "/tasks")[0].body).toMatchObject({ name: "load", description: "loads things" });
  });

  it("shows the server's refusal to delete a task that's still in use", async () => {
    const t = b.addTask("extract");
    b.failNext.set(`DELETE /tasks/${t.id}`, { status: 409, error: "this task is still referenced by a pipeline version; it cannot be removed" });
    renderApp(b, { path: "/pipeline/tasks" });
    await userEvent.click(await screen.findByRole("button", { name: "More actions for extract" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Delete task…" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete task" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("still referenced by a pipeline version");
    expect(screen.getByRole("link", { name: "extract" })).toBeInTheDocument();
  });

  it("offers viewers no write controls", async () => {
    b.addTask("extract");
    renderApp(b, { path: "/pipeline/tasks", role: "viewer" });
    await screen.findByRole("link", { name: "extract" });
    expect(screen.queryByRole("button", { name: /New task/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /More actions/ })).not.toBeInTheDocument();
  });
});
