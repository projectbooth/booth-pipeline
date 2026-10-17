import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// ADR 0119 amendment 3 / ADR 0120: who a pipeline's scheduled runs act as, and Take over.

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
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

  it("offers a workspace owner Take over from a service account", async () => {
    const p = owned({ kind: "service", id: "svc:acme:nfl-weekly", name: "nfl-weekly", you: false });
    renderApp(b, { path: schedule(p.id), role: "owner" });
    expect(within(await runsAsRow()).getByRole("button", { name: "Take over" })).toBeInTheDocument();
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
});
