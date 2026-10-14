import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseRequirements } from "../task/RequirementsField";
import type { TaskConfig } from "../types";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// ADR 0115: a task's own Python packages, edited in the task's settings.

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
});
afterEach(() => vi.unstubAllGlobals());

const cfg = (over: Partial<TaskConfig> = {}): TaskConfig => ({
  code: { type: "storage", backendId: "b1", path: "jobs/scrape.py", language: "python", source: "import requests\n" },
  runner: "base",
  retry: null,
  params: {},
  timeoutSeconds: 600,
  platformAccess: false,
  ...over,
});

const path = (id: string, tab = "") => `/pipeline/tasks/${id}${tab ? `/${tab}` : ""}`;

async function editCard(id: string) {
  renderApp(b, { path: path(id) });
  await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
  return screen.getByRole("region", { name: "Edit task settings" });
}

describe("parseRequirements", () => {
  it("keeps one trimmed requirement per line and drops blank lines and comments", () => {
    expect(parseRequirements("requests==2.32.3\n\n  # for parsing\n beautifulsoup4 \r\n")).toEqual(["requests==2.32.3", "beautifulsoup4"]);
    expect(parseRequirements("")).toEqual([]);
  });
});

describe("Python packages in the task settings", () => {
  it("saves one requirement per line, without blank lines or comments", async () => {
    const t = b.addTask("scrape", cfg());
    const card = await editCard(t.id);
    const field = within(card).getByLabelText("Python packages");
    await userEvent.type(field, "requests==2.32.3{enter}{enter}# html{enter}beautifulsoup4");
    await userEvent.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(b.called("PUT", `/tasks/${t.id}/draft`)).toHaveLength(1));
    expect(b.called("PUT", `/tasks/${t.id}/draft`)[0].body).toMatchObject({ config: { requirements: ["requests==2.32.3", "beautifulsoup4"] } });
    // What was typed stays as typed while editing.
    expect(field).toHaveValue("requests==2.32.3\n\n# html\nbeautifulsoup4");
  });

  it("shows the server's refusal of an entry on the field itself", async () => {
    const t = b.addTask("scrape", cfg({ requirements: ["requests"] }));
    const message = "'x @ https://evil.example/x.whl': direct references (name @ URL) are not allowed; packages come from the install's package index";
    b.failNext.set(`PUT /tasks/${t.id}/draft`, { status: 422, error: message, field: "requirements[1]" });
    const card = await editCard(t.id);
    await userEvent.type(within(card).getByLabelText("Python packages"), "{enter}x @ https://evil.example/x.whl");
    await userEvent.click(within(card).getByRole("button", { name: "Save" }));
    const field = within(card).getByLabelText("Python packages");
    await waitFor(() => expect(field).toHaveAttribute("aria-invalid", "true"));
    expect(within(card).getAllByText(message)).toHaveLength(1); // on the field, not also in a banner
  });

  it("warns, with the operator's reason, when this install can't install packages, and still lets you edit", async () => {
    b.pip = {
      available: false,
      reason: "this install has no route to a package index: set runner.pip.egress for an in-cluster index, or runner.networkPolicy.egress.allowInternet=true for PyPI (ADR 0115)",
    };
    const t = b.addTask("scrape", cfg());
    const card = await editCard(t.id);
    expect(await within(card).findByText(/This install can't install packages: this install has no route to a package index/)).toBeInTheDocument();
    expect(within(card).getByLabelText("Python packages")).toBeEnabled();
  });

  it("gives no warning when packages can be installed", async () => {
    const t = b.addTask("scrape", cfg());
    const card = await editCard(t.id);
    await waitFor(() => expect(b.called("GET", "/config")).toHaveLength(1));
    expect(within(card).queryByText(/can't install packages/)).not.toBeInTheDocument();
  });

  it("is not offered for a SQL task", async () => {
    const t = b.addTask("report", cfg({ code: { type: "storage", backendId: "b1", path: "jobs/report.sql", language: "sql", source: "select 1" } }));
    const card = await editCard(t.id);
    expect(within(card).queryByLabelText("Python packages")).not.toBeInTheDocument();
  });

  it("lists a task's packages in its configuration", async () => {
    const t = b.addTask("scrape", cfg({ requirements: ["requests==2.32.3", "beautifulsoup4"] }));
    renderApp(b, { path: path(t.id, "config") });
    const settings = await screen.findByRole("region", { name: "Task settings" });
    const value = within(settings).getByText("Python packages").nextElementSibling;
    expect(value?.textContent).toBe("requests==2.32.3\nbeautifulsoup4");
  });
});
