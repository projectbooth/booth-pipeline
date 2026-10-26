import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { envFor } from "../task/SecretsField";
import type { TaskConfig } from "../types";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// ADR 0128: the workspace's task secrets page, a task's declaration of them, and their names on a run.

const VALUE = "fmp-live-0123456789abcdef";

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
});
afterEach(() => vi.unstubAllGlobals());

const cfg = (over: Partial<TaskConfig> = {}): TaskConfig => ({
  code: { type: "storage", backendId: "b1", path: "jobs/capture.py", language: "python", source: "import os\n" },
  runner: "base",
  retry: null,
  params: {},
  timeoutSeconds: 600,
  platformAccess: false,
  ...over,
});

const nav = () => screen.getByRole("navigation", { name: "Pipeline sections" });

describe("Secrets page", () => {
  it("lets an owner add a secret whose value is sent once and never shown", async () => {
    renderApp(b, { path: "/pipeline/secrets", role: "owner" });
    expect(await screen.findByText("No secrets yet")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "New secret" }));
    const form = screen.getByRole("region", { name: "New secret" });
    await userEvent.type(within(form).getByLabelText("Name"), "FMP");
    const value = within(form).getByLabelText("Value");
    expect(value).toHaveAttribute("type", "password");
    await userEvent.type(value, "short");
    expect(within(form).getByText(/At least 8 characters/)).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Save" })).toBeDisabled();
    await userEvent.clear(value);
    await userEvent.type(value, VALUE);
    await userEvent.click(within(form).getByRole("button", { name: "Save" }));
    const table = await screen.findByRole("table", { name: "Secrets" });
    expect(within(table).getByText("FMP")).toBeInTheDocument();
    expect(b.called("PUT", "/secrets/FMP")[0].body).toEqual({ value: VALUE });
    expect(document.body.textContent).not.toContain(VALUE);
    expect(screen.getByText(/can't catch a value split across two log lines/)).toBeInTheDocument();
    expect(screen.getByText(/If that key is lost, every secret here becomes unreadable/)).toBeInTheDocument();
  });

  it("replaces and deletes, with a confirmation", async () => {
    b.secrets!.set("FMP", { value: VALUE, updatedBy: "olga", updatedAt: "2026-10-25T12:00:00+00:00" });
    renderApp(b, { path: "/pipeline/secrets", role: "owner" });
    await userEvent.click(await screen.findByRole("button", { name: "Replace FMP" }));
    const form = screen.getByRole("region", { name: "Replace FMP" });
    expect(within(form).getByLabelText("Name")).toBeDisabled();
    expect(within(form).getByLabelText("New value")).toHaveValue(""); // the old value is never loaded
    await userEvent.type(within(form).getByLabelText("New value"), "fmp-live-rotated-9999");
    await userEvent.click(within(form).getByRole("button", { name: "Replace" }));
    await waitFor(() => expect(b.secrets!.get("FMP")!.value).toBe("fmp-live-rotated-9999"));
    await userEvent.click(screen.getByRole("button", { name: "Delete FMP" }));
    await userEvent.click(within(screen.getByRole("group", { name: "Confirm" })).getByRole("button", { name: "Delete" }));
    expect(await screen.findByText("No secrets yet")).toBeInTheDocument();
  });

  it("shows editors the names only, and viewers no Secrets section at all", async () => {
    b.secrets!.set("FMP", { value: VALUE, updatedBy: "olga", updatedAt: "2026-10-25T12:00:00+00:00" });
    const { unmount } = renderApp(b, { path: "/pipeline/secrets", role: "editor" });
    expect(await within(await screen.findByRole("table", { name: "Secrets" })).findByText("FMP")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New secret" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Replace FMP" })).not.toBeInTheDocument();
    unmount();
    renderApp(b, { path: "/pipeline/pipelines", role: "viewer" });
    await screen.findByRole("navigation", { name: "Pipeline sections" });
    expect(within(nav()).queryByRole("link", { name: "Secrets" })).not.toBeInTheDocument();
  });

  it("says when this install has no key", async () => {
    b.secrets = null;
    renderApp(b, { path: "/pipeline/secrets", role: "owner" });
    expect(await screen.findByText(/task secrets are not set up on this install/)).toBeInTheDocument();
  });
});

describe("A task's secrets", () => {
  it("suggests an environment variable from a secret's name", () => {
    expect(envFor("FMP")).toBe("FMP");
    expect(envFor("fmp-backup")).toBe("FMP_BACKUP");
    expect(envFor("1st")).toBe("_1ST");
  });

  it("declares a workspace secret as an environment variable and saves it with the task", async () => {
    b.secrets!.set("FMP", { value: VALUE, updatedBy: "olga", updatedAt: "2026-10-25T12:00:00+00:00" });
    const t = b.addTask("capture", cfg());
    renderApp(b, { path: `/pipeline/tasks/${t.id}` });
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const card = screen.getByRole("region", { name: "Edit task settings" });
    const section = within(card).getByRole("region", { name: "Secrets" });
    await waitFor(() => expect(b.called("GET", "/secrets")).toHaveLength(1));
    await userEvent.click(within(section).getByRole("button", { name: "Add a secret" }));
    expect(within(section).getByLabelText("Secret")).toHaveValue("FMP"); // the only one: chosen for you
    const env = within(section).getByLabelText("Environment variable for FMP");
    expect(env).toHaveValue("FMP");
    await userEvent.clear(env);
    await userEvent.type(env, "FMP_API_KEY");
    await userEvent.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(b.called("PUT", `/tasks/${t.id}/draft`)).toHaveLength(1));
    expect(b.called("PUT", `/tasks/${t.id}/draft`)[0].body).toMatchObject({ config: { secrets: [{ name: "FMP", env: "FMP_API_KEY" }] } });
  });

  it("shows the server's refusal on the secret's row", async () => {
    const t = b.addTask("capture", cfg({ secrets: [{ name: "FMP", env: "PATH" }] }));
    b.failNext.set(`PUT /tasks/${t.id}/draft`, { status: 422, error: "PATH is set by the runner or changes how the task's process runs; choose another name", field: "secrets[0].env" });
    renderApp(b, { path: `/pipeline/tasks/${t.id}` });
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const card = screen.getByRole("region", { name: "Edit task settings" });
    const section = within(card).getByRole("region", { name: "Secrets" });
    await userEvent.type(within(section).getByLabelText("Environment variable for FMP"), "S"); // PATHS: still refused by this fake
    await userEvent.click(within(card).getByRole("button", { name: "Save" }));
    expect(await within(section).findByText(/PATH is set by the runner/)).toBeInTheDocument();
    expect(within(card).getAllByText(/PATH is set by the runner/)).toHaveLength(1); // not also in a banner
  });

  it("isn't offered on a Spark task", async () => {
    const t = b.addTask("agg", cfg({ runner: "spark", spark: {} }));
    renderApp(b, { path: `/pipeline/tasks/${t.id}` });
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const card = screen.getByRole("region", { name: "Edit task settings" });
    expect(within(card).queryByRole("region", { name: "Secrets" })).not.toBeInTheDocument();
  });
});

describe("A run's secrets", () => {
  it("lists the names each task was given, never a value", async () => {
    const p = b.addPipeline("capture", [b.ref("capture")]);
    const r = b.addRun(p.id, "succeeded", "2026-10-28T13:15:04+00:00", {
      tasks: [
        { taskKey: "capture", status: "succeeded", attempts: 1, startedAt: "2026-10-28T13:15:05+00:00", finishedAt: "2026-10-28T13:15:06+00:00", error: null, secrets: ["FMP"] },
      ],
    });
    renderApp(b, { path: `/pipeline/runs/${r.id}` });
    const table = await screen.findByRole("table", { name: "Tasks in this run" });
    expect(within(table).getByText("Secrets: FMP")).toBeInTheDocument();
  });
});
