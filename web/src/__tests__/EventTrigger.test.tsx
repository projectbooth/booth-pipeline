import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { EventTrigger } from "../types";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// ADR 0123: the event trigger on the Schedule & Triggers tab, and event-fired runs.

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
});
afterEach(() => vi.unstubAllGlobals());

const path = (id: string, tab = "") => `/pipeline/pipelines/${id}${tab ? `/${tab}` : ""}`;

const INBOX: EventTrigger = {
  type: "event",
  enabled: true,
  match: [{ storage: { backendId: "b1", prefix: "finance/dev/inbox/" } }, { api: { slugPrefix: "finance-dev-" } }],
  debounceSeconds: 10,
  maxWaitSeconds: 60,
  whenRunning: "queue_one",
  minGapSeconds: 5,
};

function silver(eventTrigger: EventTrigger | null | undefined = INBOX) {
  const p = b.addPipeline("silver", [b.ref("load")]);
  p.eventTrigger = eventTrigger;
  return p;
}

async function openEditor(id: string) {
  renderApp(b, { path: path(id, "schedule") });
  await userEvent.click(await screen.findByRole("button", { name: "Edit schedule" }));
  return screen.getByRole("form", { name: "Pipeline schedule" });
}

describe("the event trigger", () => {
  it("summarises what it watches, its timing, and a live status line", async () => {
    const p = silver();
    b.eventTriggerStatus.set(p.id, {
      lastEventAt: "2026-10-21T14:02:11+00:00",
      lastEvent: { type: "storage.object.created", publishedAt: "2026-10-21T14:02:11Z", principal: "streamlit:acme:finance-dev", backendId: "b1", path: "finance/dev/inbox/stmt.csv" },
      nextFireAt: new Date(Date.now() + 7000).toISOString(),
      pendingEvents: 3,
      ignoredOwnRuns: 2,
    });
    renderApp(b, { path: path(p.id, "schedule") });
    const card = await screen.findByRole("region", { name: "Event trigger" });
    expect(card).toHaveTextContent("Files in b1:finance/dev/inbox/");
    expect(card).toHaveTextContent("Writes to APIs starting finance-dev-");
    expect(card).toHaveTextContent("Runs 10 s after the last event, at most 1 min after the first");
    expect(card).toHaveTextContent("If a run is active, it runs once more afterwards.");
    const status = await within(card).findByLabelText("Event trigger status");
    expect(status).toHaveTextContent("b1:finance/dev/inbox/stmt.csv (streamlit:acme:finance-dev)");
    expect(status).toHaveTextContent("(3 events waiting)");
    expect(status).toHaveTextContent("Ignored 2 events written by this pipeline's own runs");
    expect(card).toHaveTextContent("Storage events can be missed while the event bus is down");
    expect(card).toHaveTextContent("does not see files a task or Spark writes straight to object storage");
    expect(await screen.findByText(/Runs on events\./)).toBeInTheDocument();
  });

  it("warns when events may have been missed, and shows a queued follow-up", async () => {
    const p = silver();
    b.eventTriggerStatus.set(p.id, { queued: true, pendingEvents: 1, missedEventsAt: "2026-10-21T10:00:00+00:00" });
    renderApp(b, { path: path(p.id, "schedule") });
    const status = await screen.findByLabelText("Event trigger status");
    expect(status).toHaveTextContent("Waiting for the active run, then runs once (1 event waiting)");
    expect(status).toHaveTextContent("Events may have been missed");
  });

  it("is set up in the editor and saved beside the schedule", async () => {
    const p = silver(null);
    const form = await openEditor(p.id);
    await userEvent.click(within(form).getByLabelText("Run when files arrive or rows are written"));
    await userEvent.selectOptions(await within(form).findByLabelText("Backend"), "b1");
    await userEvent.type(within(form).getByLabelText("Folder"), "finance/dev/inbox/");
    await userEvent.click(within(form).getByRole("button", { name: "Watch something else" }));
    const second = within(form).getAllByRole("listitem")[1];
    await userEvent.selectOptions(within(second).getByLabelText("Watch"), "api");
    await userEvent.selectOptions(within(second).getByLabelText("APIs"), "prefix");
    await userEvent.type(within(second).getByLabelText("Slug starts with"), "finance-dev-");
    await userEvent.click(within(form).getByLabelText("Ignore events that arrive during it"));
    await userEvent.click(within(form).getByRole("button", { name: "Save schedule" }));
    expect(await screen.findByText("Schedule saved.")).toBeInTheDocument();
    expect(b.called("PUT", `/pipelines/${p.id}/schedule`)[0].body).toMatchObject({
      schedule: null,
      eventTrigger: {
        type: "event",
        enabled: true,
        match: [{ storage: { backendId: "b1", prefix: "finance/dev/inbox/" } }, { api: { slugPrefix: "finance-dev-" } }],
        debounceSeconds: 10,
        maxWaitSeconds: 60,
        whenRunning: "skip",
        minGapSeconds: 5,
      },
    });
  });

  it("removes it when unticked, and hides 'if a run is active' when runs may overlap", async () => {
    const p = silver();
    p.allowConcurrentRuns = true;
    const form = await openEditor(p.id);
    expect(within(form).getByText("Overlapping runs are allowed, so each burst starts its own run.")).toBeInTheDocument();
    expect(within(form).queryByLabelText("Ignore events that arrive during it")).not.toBeInTheDocument();
    await userEvent.click(within(form).getByLabelText("Run when files arrive or rows are written"));
    await userEvent.click(within(form).getByRole("button", { name: "Save schedule" }));
    await screen.findByText("Schedule saved.");
    expect((b.called("PUT", `/pipelines/${p.id}/schedule`)[0].body as { eventTrigger?: unknown }).eventTrigger).toBeNull();
  });

  it("shows a backend error under its field", async () => {
    const p = silver();
    b.failNext.set(`PUT /pipelines/${p.id}/schedule`, { status: 422, error: "this workspace has no Storage backend 'b1'", field: "eventTrigger.match[0].storage.backendId" });
    const form = await openEditor(p.id);
    await userEvent.click(within(form).getByRole("button", { name: "Save schedule" }));
    const backend = await within(form).findByLabelText("Backend");
    await waitFor(() => expect(backend).toHaveAccessibleDescription(/no Storage backend 'b1'/));
  });

  it("says when this install can't turn one on", async () => {
    b.eventTriggersAvailable = false;
    const p = silver(null);
    const form = await openEditor(p.id);
    await userEvent.click(within(form).getByLabelText("Run when files arrive or rows are written"));
    expect(await within(form).findByText(/no event-bus credential/)).toBeInTheDocument();
  });

  it("is absent, and never sent, on a backend that doesn't have it", async () => {
    const p = silver();
    delete p.eventTrigger; // a backend without event triggers sends no such field
    const form = await openEditor(p.id);
    expect(within(form).queryByLabelText("Run when files arrive or rows are written")).not.toBeInTheDocument();
    await userEvent.click(within(form).getByRole("button", { name: "Save schedule" }));
    await screen.findByText("Schedule saved.");
    expect("eventTrigger" in (b.called("PUT", `/pipelines/${p.id}/schedule`)[0].body as object)).toBe(false);
    expect(screen.queryByRole("region", { name: "Event trigger" })).not.toBeInTheDocument();
  });
});

describe("an event-fired run", () => {
  it("says what fired it, with the events for editors", async () => {
    const p = silver();
    const r = b.addRun(p.id, "succeeded", "2026-10-21T14:03:00+00:00", {
      trigger: "event",
      triggeredBy: "2 files",
      triggerEvents: {
        count: 2,
        missedEvents: false,
        events: [
          { type: "storage.object.created", publishedAt: "", principal: "u", backendId: "b1", path: "finance/dev/inbox/a.csv" },
          { type: "storage.object.created", publishedAt: "", principal: "u", backendId: "b1", path: "finance/dev/inbox/b.csv" },
        ],
      },
    });
    renderApp(b, { path: `/pipeline/runs/${r.id}` });
    expect(await screen.findByText("Event — 2 files")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Trigger events" })).toHaveTextContent("b1:finance/dev/inbox/a.csv");
  });

  it("shows viewers the count only", async () => {
    const p = silver();
    const r = b.addRun(p.id, "succeeded", "2026-10-21T14:03:00+00:00", { trigger: "event", triggeredBy: "2 files", triggerEvents: { count: 2, missedEvents: false } });
    renderApp(b, { path: `/pipeline/runs/${r.id}`, role: "viewer" });
    expect(await screen.findByText("Event — 2 files")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Trigger events" })).not.toBeInTheDocument();
  });
});
