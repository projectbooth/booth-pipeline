import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiContext } from "../api/client";
import { latestRunFor, latestRunsFor } from "../runs";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// A scheduled fire that finds the previous run active is recorded as a newer skipped run. It must not hide the
// run that is still going, on the list or the pipeline's page, nor stop the list polling.

const ctx: ApiContext = { workspace: "acme", getAccessToken: () => "tok" };

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const row = (name: string) => within(screen.getByRole("table")).getByRole("link", { name }).closest("tr")!;

function busy(status: "running" | "queued" = "running", dropped = 2) {
  const p = b.addPipeline("finance-dev-silver", [b.ref("load")]);
  const going = b.addRun(p.id, status, "2026-11-01T12:00:00+00:00");
  for (let i = 1; i <= dropped; i++) {
    b.addRun(p.id, "skipped", `2026-11-01T12:0${i}:00+00:00`, { error: "previous run still active" });
  }
  return { p, going };
}

describe("latestRunsFor", () => {
  it("prefers a running or queued run over a newer skipped row", async () => {
    const running = busy("running");
    const queued = busy("queued");
    b.install();
    const { latest, anyActive } = await latestRunsFor(ctx, [running.p.id, queued.p.id]);
    expect(latest[running.p.id]?.id).toBe(running.going.id);
    expect(latest[queued.p.id]?.id).toBe(queued.going.id);
    expect(anyActive).toBe(true);
  });

  it("finds a run behind more skipped rows than one page holds", async () => {
    const p = b.addPipeline("long", [b.ref("load")]);
    const going = b.addRun(p.id, "running", "2026-01-01T00:00:00+00:00");
    for (let i = 0; i < 201; i++) b.addRun(p.id, "skipped", new Date(Date.UTC(2026, 8, 1, 0, i)).toISOString());
    b.install();
    expect((await latestRunsFor(ctx, [p.id])).latest[p.id]?.id).toBe(going.id);
  });

  it("keeps a skipped row when nothing is going", async () => {
    const p = b.addPipeline("quiet", [b.ref("load")]);
    b.addRun(p.id, "succeeded", "2026-11-01T11:00:00+00:00");
    const skipped = b.addRun(p.id, "skipped", "2026-11-01T12:00:00+00:00");
    b.install();
    const { latest, anyActive } = await latestRunsFor(ctx, [p.id]);
    expect(latest[p.id]?.id).toBe(skipped.id);
    expect(anyActive).toBe(false);
  });

  it("only overrides a skipped row: a newer finished run still shows, and the list still knows a run is going", async () => {
    const p = b.addPipeline("concurrent", [b.ref("load")]);
    b.addRun(p.id, "running", "2026-11-01T11:00:00+00:00");
    const done = b.addRun(p.id, "succeeded", "2026-11-01T12:00:00+00:00");
    b.install();
    const { latest, anyActive } = await latestRunsFor(ctx, [p.id]);
    expect(latest[p.id]?.id).toBe(done.id);
    expect(anyActive).toBe(true);
  });

  it("ignores another pipeline's active run", async () => {
    busy();
    const other = b.addPipeline("other", [b.ref("load")]);
    const done = b.addRun(other.id, "succeeded", "2026-11-01T12:00:00+00:00");
    b.install();
    const { latest, anyActive } = await latestRunsFor(ctx, [other.id]);
    expect(latest[other.id]?.id).toBe(done.id);
    expect(anyActive).toBe(false);
  });
});

describe("latestRunFor", () => {
  it("prefers the active run, and asks no more than it needs", async () => {
    const { p, going } = busy();
    b.install();
    expect((await latestRunFor(ctx, p.id))?.id).toBe(going.id);

    const q = b.addPipeline("plain", [b.ref("load")]);
    const done = b.addRun(q.id, "succeeded", "2026-11-01T12:00:00+00:00");
    b.calls.length = 0;
    expect((await latestRunFor(ctx, q.id))?.id).toBe(done.id);
    expect(b.calls).toHaveLength(1);
  });

  it("returns null for a pipeline that has never run", async () => {
    const p = b.addPipeline("never", [b.ref("load")]);
    b.install();
    expect(await latestRunFor(ctx, p.id)).toBeNull();
  });
});

describe("The screens", () => {
  it("shows the list's busy pipeline as Running and under the Running filter, not Skipped", async () => {
    busy();
    renderApp(b);
    await screen.findByRole("link", { name: "finance-dev-silver" });
    expect(within(row("finance-dev-silver")).getByText("Running")).toBeInTheDocument();
    expect(within(row("finance-dev-silver")).queryByText("Skipped")).not.toBeInTheDocument();
    const filters = screen.getByRole("group", { name: "Filter by last run status" });
    await userEvent.click(within(filters).getByRole("button", { name: /Running 1/ }));
    expect(screen.getByRole("link", { name: "finance-dev-silver" })).toBeInTheDocument();
  });

  it("shows the pipeline's page as Running, from the run that is going", async () => {
    const { p, going } = busy();
    renderApp(b, { path: `/pipeline/pipelines/${p.id}` });
    await screen.findByRole("heading", { name: "finance-dev-silver" });
    await waitFor(() => expect(b.called("GET", `/runs/${going.id}`).length).toBeGreaterThan(0));
    const header = screen.getByRole("heading", { name: "finance-dev-silver" }).parentElement!;
    await waitFor(() => expect(within(header).getByText("Running")).toBeInTheDocument());
  });

  async function pollsAfterTenSeconds(): Promise<boolean> {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderApp(b);
    await screen.findByRole("table");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    const before = b.called("GET", "/pipelines").length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_500);
    });
    return b.called("GET", "/pipelines").length > before;
  }

  it("keeps the list polling while a run is going behind a skipped row", async () => {
    busy();
    expect(await pollsAfterTenSeconds()).toBe(true);
  });

  it("keeps the list polling while a run is going behind a newer finished one", async () => {
    const p = b.addPipeline("concurrent", [b.ref("load")]);
    b.addRun(p.id, "running", "2026-11-01T11:00:00+00:00");
    b.addRun(p.id, "succeeded", "2026-11-01T12:00:00+00:00");
    expect(await pollsAfterTenSeconds()).toBe(true);
  });

  it("stops polling when nothing is going", async () => {
    const p = b.addPipeline("quiet", [b.ref("load")]);
    b.addRun(p.id, "skipped", "2026-11-01T12:00:00+00:00");
    expect(await pollsAfterTenSeconds()).toBe(false);
  });
});
