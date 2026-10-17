import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAccountCache } from "../views/pipeline/RunsAs";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// ADR 0119 amendment 3 / ADR 0120: who a pipeline's scheduled runs act as, and Take over.

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
  clearAccountCache();
  b.serviceAccounts = [
    { id: "svc:acme:nfl-weekly", name: "nfl-weekly", role: "editor", description: "the weekly NFL load", disabled: false, createdBy: "u-owner", createdAt: "2026-10-16T00:00:00Z" },
    { id: "svc:acme:old-bot", name: "old-bot", role: "viewer", description: "", disabled: true, createdBy: "u-owner", createdAt: "2026-10-16T00:00:00Z" },
  ];
});
afterEach(() => vi.unstubAllGlobals());

function owned(owner: { kind: "person" | "service"; id?: string; name: string; you: boolean }, name = "nfl") {
  const p = b.addPipeline(name);
  p.schedule = { type: "cron", cron: "0 9 * * 1", timezone: "UTC", enabled: true };
  p.hasOwner = true;
  p.owner = owner;
  return p;
}

const schedule = (id: string) => `/pipeline/pipelines/${id}/schedule`;

async function runsAsRow() {
  const label = await screen.findByText("Runs as");
  return label.parentElement as HTMLElement;
}

describe("Runs as", () => {
  it("names a person owner and lets an editor take the pipeline over, after saying what that means", async () => {
    const p = owned({ kind: "person", name: "olga", you: false });
    renderApp(b, { path: schedule(p.id), role: "editor" });
    const row = await runsAsRow();
    expect(row).toHaveTextContent("olga");
    await userEvent.click(within(row).getByRole("button", { name: "Take over" }));
    const confirm = within(row).getByRole("group", { name: "Take over this pipeline" });
    expect(confirm).toHaveTextContent("act as you");
    await userEvent.click(within(confirm).getByRole("button", { name: "Take over" }));
    await waitFor(() => expect(b.called("PUT", `/pipelines/${p.id}/owner`)).toHaveLength(1));
    expect(b.called("PUT", `/pipelines/${p.id}/owner`)[0].body).toEqual({ owner: "me" });
    expect(await within(await runsAsRow()).findByText("eddie (you)")).toBeInTheDocument();
  });

  it("offers no Take over to the owner themselves, or to a viewer", async () => {
    const mine = owned({ kind: "person", name: "eddie", you: true }, "mine");
    renderApp(b, { path: schedule(mine.id), role: "editor" });
    expect(within(await runsAsRow()).queryByRole("button", { name: "Take over" })).not.toBeInTheDocument();
  });

  it("offers no Take over to a viewer", async () => {
    const p = owned({ kind: "person", name: "olga", you: false });
    renderApp(b, { path: schedule(p.id), role: "viewer" });
    expect(within(await runsAsRow()).queryByRole("button", { name: "Take over" })).not.toBeInTheDocument();
  });

  it("lets only a workspace owner take a pipeline from a service account", async () => {
    const p = owned({ kind: "service", id: "svc:acme:nfl-weekly", name: "nfl-weekly", you: false });
    renderApp(b, { path: schedule(p.id), role: "editor" });
    const row = await runsAsRow();
    expect(row).toHaveTextContent("Service account svc:acme:nfl-weekly");
    expect(within(row).queryByRole("button", { name: "Take over" })).not.toBeInTheDocument();
  });

  it("lets a workspace owner take a pipeline from a service account, with the picker", async () => {
    const p = owned({ kind: "service", id: "svc:acme:nfl-weekly", name: "nfl-weekly", you: false });
    renderApp(b, { path: schedule(p.id), role: "owner" });
    const picker = within(await runsAsRow()).getByRole("group", { name: "Choose who this pipeline runs as" });
    await userEvent.selectOptions(within(picker).getByLabelText("Runs as"), "me");
    await userEvent.click(within(picker).getByRole("button", { name: "Run as this" }));
    await waitFor(() => expect(b.called("PUT", `/pipelines/${p.id}/owner`)[0]?.body).toEqual({ owner: "me" }));
  });

  it("lists which pipelines run as each service account", async () => {
    owned({ kind: "service", id: "svc:acme:nfl-weekly", name: "nfl-weekly", you: false }, "nfl");
    owned({ kind: "service", id: "svc:acme:nfl-weekly", name: "nfl-weekly", you: false }, "nfl-backfill");
    owned({ kind: "person", name: "olga", you: false }, "adhoc");
    renderApp(b);
    const section = await screen.findByRole("region", { name: "Service accounts in use" });
    expect(section).toHaveTextContent("svc:acme:nfl-weekly: nfl, nfl-backfill");
    expect(section).not.toHaveTextContent("adhoc");
    expect(within(screen.getByRole("table")).getAllByText("Service account svc:acme:nfl-weekly")).toHaveLength(2);
  });

  it("shows no usage section when no pipeline runs as a service account", async () => {
    owned({ kind: "person", name: "olga", you: false });
    renderApp(b);
    await screen.findByRole("table");
    await waitFor(() => expect(b.called("GET", "/service-accounts/uses")).toHaveLength(1));
    expect(screen.queryByRole("region", { name: "Service accounts in use" })).not.toBeInTheDocument();
  });

  it("gives a workspace owner the picker, listing core's accounts with role and description, disabled ones not choosable", async () => {
    const p = owned({ kind: "person", name: "olga", you: false });
    renderApp(b, { path: schedule(p.id), role: "owner" });
    const picker = within(await runsAsRow()).getByRole("group", { name: "Choose who this pipeline runs as" });
    const select = within(picker).getByLabelText("Runs as");
    const nfl = await within(select).findByRole("option", { name: "Service account nfl-weekly — editor — the weekly NFL load" });
    expect(nfl).toBeEnabled();
    expect(within(select).getByRole("option", { name: "Service account old-bot — viewer (disabled)" })).toBeDisabled();
    expect(within(await runsAsRow()).queryByRole("button", { name: "Take over" })).not.toBeInTheDocument(); // the picker's "Me" is that
    await userEvent.selectOptions(select, "svc:acme:nfl-weekly");
    await userEvent.click(within(picker).getByRole("button", { name: "Run as this" }));
    await waitFor(() => expect(b.called("PUT", `/pipelines/${p.id}/owner`)[0]?.body).toEqual({ owner: "svc:acme:nfl-weekly" }));
    expect(await within(await runsAsRow()).findByText("Service account svc:acme:nfl-weekly")).toBeInTheDocument();
  });

  it("an editor gets no picker and never asks for the owner-only list", async () => {
    const p = owned({ kind: "person", name: "olga", you: false });
    renderApp(b, { path: schedule(p.id), role: "editor" });
    await runsAsRow();
    expect(screen.queryByRole("group", { name: "Choose who this pipeline runs as" })).not.toBeInTheDocument();
    expect(b.called("GET", "/api/service-accounts")).toHaveLength(0);
  });

  it("says so when core won't list the accounts", async () => {
    b.serviceAccountsListError = { status: 404, error: "404 page not found" };
    const p = owned({ kind: "person", name: "olga", you: false });
    renderApp(b, { path: schedule(p.id), role: "owner" });
    expect(await within(await runsAsRow()).findByText(/Service accounts couldn't be listed \(404 page not found\)/)).toBeInTheDocument();
  });

  it("badges a disabled owner and a deleted one, for every member, and not an enabled one", async () => {
    const off = owned({ kind: "service", id: "svc:acme:old-bot", name: "old-bot", you: false }, "off");
    renderApp(b, { path: schedule(off.id), role: "viewer" });
    const badge = await within(await runsAsRow()).findByRole("status");
    expect(badge).toHaveTextContent("Owner disabled");
    expect(badge.getAttribute("title")).toMatch(/scheduled runs fail at their start/);
  });

  it("badges an owner core no longer has", async () => {
    const gone = owned({ kind: "service", id: "svc:acme:deleted", name: "deleted", you: false }, "gone");
    renderApp(b, { path: schedule(gone.id), role: "viewer" });
    expect(await within(await runsAsRow()).findByRole("status")).toHaveTextContent("Owner not found");
  });

  it("puts no badge on an enabled account, and asks core once per account on the list", async () => {
    owned({ kind: "service", id: "svc:acme:nfl-weekly", name: "nfl-weekly", you: false }, "a");
    owned({ kind: "service", id: "svc:acme:nfl-weekly", name: "nfl-weekly", you: false }, "b");
    owned({ kind: "service", id: "svc:acme:old-bot", name: "old-bot", you: false }, "c");
    renderApp(b, { role: "viewer" });
    const table = await screen.findByRole("table");
    await waitFor(() => expect(within(table).getAllByRole("status")).toHaveLength(1));
    expect(within(table).getByRole("status")).toHaveTextContent("Owner disabled");
    expect(b.called("GET", "/api/service-accounts/nfl-weekly")).toHaveLength(1);
  });
});
