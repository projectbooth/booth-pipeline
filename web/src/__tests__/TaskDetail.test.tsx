import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TaskConfig } from "../types";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// S5 (docs/decisions/0016 §4, phase 4).

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
});
afterEach(() => vi.unstubAllGlobals());

const cfg = (over: Partial<TaskConfig> = {}): TaskConfig => ({
  code: { type: "storage", backendId: "b1", path: "jobs/extract.py", language: "python", source: "def run(ctx):\n    return 42\n" },
  runner: "base",
  retry: { maxRetries: 2, delaySeconds: 5, backoff: "exponential" },
  params: { source_bucket: "s3://raw/" },
  timeoutSeconds: 1800,
  platformAccess: true,
  ...over,
});

const path = (id: string, tab = "", version?: number) => `/pipeline/tasks/${id}${tab ? `/${tab}` : ""}${version ? `?version=${version}` : ""}`;

describe("task page", () => {
  it("shows the header with language, version state and meta chips", async () => {
    const t = b.addTask("extract", cfg(), "Reads raw events");
    renderApp(b, { path: path(t.id) });
    expect(await screen.findByRole("heading", { name: "extract" })).toBeInTheDocument();
    expect(screen.getByText("Reads raw events")).toBeInTheDocument();
    expect(screen.getByText("PY")).toBeInTheDocument();
    expect(screen.getByText("v1")).toBeInTheDocument();
    const chips = screen.getByRole("list", { name: "Task summary" });
    expect(within(chips).getByText("1800s")).toBeInTheDocument();
    expect(within(chips).getByText("2 · exponential")).toBeInTheDocument();
    expect(screen.queryByText(/Draft ahead/)).not.toBeInTheDocument();
  });

  it("Code tab shows where the code comes from and the saved source, line-numbered", async () => {
    const t = b.addTask("extract", cfg());
    renderApp(b, { path: path(t.id) });
    const code = await screen.findByRole("region", { name: "Code" });
    expect(within(code).getByText(/storage: b1:jobs\/extract\.py/)).toBeInTheDocument();
    const src = within(code).getByLabelText("Source");
    expect(src).toHaveTextContent("def run(ctx):");
    expect(src).toHaveTextContent("return 42");
    expect(within(code).getByRole("button", { name: "Copy" })).toBeInTheDocument();
  });

  it("Configuration tab is flat and read-only, with parameters", async () => {
    const t = b.addTask("extract", cfg());
    renderApp(b, { path: path(t.id, "config") });
    const settings = await screen.findByRole("region", { name: "Task settings" });
    expect(within(settings).getByText(t.id)).toBeInTheDocument();
    expect(within(settings).getByText(/2 \(exponential, 5s delay\)/)).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Parameters" })).getByText("s3://raw/")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("shows any version by number, with creation times, and flags a differing draft", async () => {
    const t = b.addTask("extract", cfg({ timeoutSeconds: 100 }));
    b.saveTaskVersion(t.id, cfg({ timeoutSeconds: 200 }), "slower");
    b.saveTaskDraft(t.id, cfg({ timeoutSeconds: 300 })); // draft ≠ v2
    renderApp(b, { path: path(t.id, "config") });
    expect(await screen.findByText("Draft ahead of v2")).toBeInTheDocument();
    expect(screen.getByText("300s", { selector: "dd" })).toBeInTheDocument(); // current = draft

    const picker = screen.getByRole("combobox", { name: "Task version shown" });
    expect(within(picker).getByRole("option", { name: "Current draft" })).toBeInTheDocument();
    await userEvent.selectOptions(picker, within(picker).getByRole("option", { name: /^v1 — \d{4}-\d{2}-\d{2} \d{2}:\d{2}$/ }));
    await waitFor(() => expect(window.location.search).toBe("?version=1"));
    expect(await screen.findByText("100s", { selector: "dd" })).toBeInTheDocument();
  });

  it("Versions tab lists the history and a Draft row only when it differs", async () => {
    const t = b.addTask("extract", cfg());
    b.saveTaskVersion(t.id, cfg({ runner: "base" }), "second");
    renderApp(b, { path: path(t.id, "versions") });
    expect(await screen.findByText("second")).toBeInTheDocument();
    expect(screen.queryByText("Draft")).not.toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("link", { name: "View" })[1]);
    expect(window.location.pathname + window.location.search).toBe(path(t.id, "", 1));
  });

  it("opens straight into Edit for an unconfigured task, and plain Save makes a draft", async () => {
    const t = b.addTask("fresh");
    renderApp(b, { path: path(t.id) });
    const card = await screen.findByRole("region", { name: "Configure this task" });
    expect(screen.getByText("Not configured yet")).toBeInTheDocument();
    await userEvent.click(within(card).getByLabelText("From storage"));
    await userEvent.click(await within(card).findByRole("button", { name: "Use tasks/a.py" }));
    await userEvent.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(b.called("PUT", `/tasks/${t.id}/draft`)).toHaveLength(1));
    expect(b.called("PUT", `/tasks/${t.id}/draft`)[0].body).toMatchObject({ config: { code: { type: "storage", backendId: "b1", path: "tasks/a.py" } } });
    expect(b.called("POST", `/tasks/${t.id}/versions`)).toHaveLength(0);
    expect(await screen.findByText("Draft only")).toBeInTheDocument();
  });

  it("Save as new version from Edit snapshots v2 with a note", async () => {
    const t = b.addTask("extract", cfg());
    renderApp(b, { path: path(t.id) });
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const card = screen.getByRole("region", { name: "Edit task settings" });
    const timeout = within(card).getByLabelText("Timeout (seconds)");
    await userEvent.clear(timeout);
    await userEvent.type(timeout, "90");
    await userEvent.click(within(card).getByRole("button", { name: "Save as new version…" }));
    await userEvent.type(within(card).getByRole("textbox", { name: "Task version note" }), "faster");
    await userEvent.click(within(card).getByRole("button", { name: "Save version" }));
    expect(await within(card).findByText(/Saved as task v2/)).toBeInTheDocument();
    expect(b.called("POST", `/tasks/${t.id}/versions`)[0].body).toMatchObject({ notes: "faster", config: { timeoutSeconds: 90 } });
    expect(await screen.findByText("v2")).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Close" }));
    expect(await screen.findByRole("region", { name: "Code" })).toBeInTheDocument();
  });

  it("asks before leaving with unsaved task edits", async () => {
    const t = b.addTask("extract", cfg());
    renderApp(b, { path: path(t.id) });
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const timeout = screen.getByLabelText("Timeout (seconds)");
    await userEvent.clear(timeout);
    await userEvent.type(timeout, "90");
    await userEvent.click(screen.getByRole("link", { name: "← Tasks" }));
    expect(screen.getByText(/unsaved changes to this task's settings/)).toBeInTheDocument();
    expect(window.location.pathname).toBe(path(t.id));
  });

  it("edits the name and description", async () => {
    const t = b.addTask("extract", cfg());
    renderApp(b, { path: path(t.id) });
    await userEvent.click(await screen.findByRole("button", { name: "More task actions" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Edit details…" }));
    const name = screen.getByLabelText(/^Name/);
    await userEvent.clear(name);
    await userEvent.type(name, "extract_v2");
    await userEvent.click(screen.getByRole("button", { name: "Save details" }));
    expect(await screen.findByRole("heading", { name: "extract_v2" })).toBeInTheDocument();
  });

  it("shows the server's refusal to delete a task still in use", async () => {
    const t = b.addTask("extract", cfg());
    b.failNext.set(`DELETE /tasks/${t.id}`, { status: 409, error: "this task is still referenced by a pipeline version; it cannot be removed" });
    renderApp(b, { path: path(t.id) });
    await userEvent.click(await screen.findByRole("button", { name: "More task actions" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Delete task…" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete task" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("still referenced by a pipeline version");
    expect(window.location.pathname).toBe(path(t.id));
  });

  it("gives viewers the page read-only", async () => {
    const t = b.addTask("extract", cfg());
    renderApp(b, { path: path(t.id), role: "viewer" });
    await screen.findByRole("region", { name: "Code" });
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "More task actions" })).not.toBeInTheDocument();
  });
});
