import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// S1 (docs/decisions/0016 §4).

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
});
afterEach(() => vi.unstubAllGlobals());

function seed() {
  const etl = b.addPipeline("daily-etl", [b.ref("extract")]);
  etl.schedule = { type: "cron", cron: "0 6 * * *", timezone: "UTC", enabled: true };
  etl.nextRunAt = "2026-09-26T06:00:00+00:00";
  b.addRun(etl.id, "succeeded", "2026-09-23T06:00:00+00:00");
  b.addRun(etl.id, "failed", "2026-09-24T06:00:00+00:00"); // newest: this is the status shown
  const churn = b.addPipeline("churn-retrain", [b.ref("train")]);
  b.addRun(churn.id, "running", "2026-09-25T06:00:00+00:00");
  const adhoc = b.addPipeline("adhoc"); // never saved, never run
  return { etl, churn, adhoc };
}

const row = (name: string) => screen.getByRole("link", { name }).closest("tr")!;

describe("Pipelines list", () => {
  it("shows one row per pipeline with its latest run's status, trigger, and owner", async () => {
    seed();
    renderApp(b);
    await screen.findByRole("link", { name: "daily-etl" });

    expect(within(row("daily-etl")).getByText("Failed")).toBeInTheDocument();
    expect(within(row("daily-etl")).getByText(/0 6 \* \* \*/)).toBeInTheDocument();
    expect(within(row("churn-retrain")).getByText("Running")).toBeInTheDocument();
    expect(within(row("churn-retrain")).getByText("Manual only")).toBeInTheDocument();
    expect(within(row("adhoc")).getByText("Never run")).toBeInTheDocument();
    expect(within(row("adhoc")).getByText("eddie")).toBeInTheDocument();
  });

  it("filters by last-run status, with counts", async () => {
    seed();
    renderApp(b);
    await screen.findByRole("link", { name: "daily-etl" });
    const filters = screen.getByRole("group", { name: "Filter by last run status" });
    expect(within(filters).getByRole("button", { name: /Failed 1/ })).toBeInTheDocument();

    await userEvent.click(within(filters).getByRole("button", { name: /Running/ }));
    expect(screen.getByRole("link", { name: "churn-retrain" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "daily-etl" })).not.toBeInTheDocument();

    await userEvent.click(within(filters).getByRole("button", { name: /Never run/ }));
    expect(screen.getByRole("link", { name: "adhoc" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "churn-retrain" })).not.toBeInTheDocument();
  });

  it("does not call a quiet pipeline 'never run' just because its last run is older than the newest 200", async () => {
    const busy = b.addPipeline("busy", [b.ref("a")]);
    const quiet = b.addPipeline("quiet", [b.ref("b")]);
    b.addRun(quiet.id, "succeeded", "2026-01-01T00:00:00+00:00");
    for (let i = 0; i < 201; i++) b.addRun(busy.id, "succeeded", new Date(Date.UTC(2026, 8, 1, 0, i)).toISOString());
    renderApp(b);
    await screen.findByRole("link", { name: "quiet" });
    await waitFor(() => expect(within(row("quiet")).getByText("Succeeded")).toBeInTheDocument());
    expect(b.calls.some((c) => c.path.includes(`pipelineId=${quiet.id}`))).toBe(true);
  });

  it("Run now starts a run and opens it", async () => {
    const { etl } = seed();
    renderApp(b);
    await userEvent.click(await screen.findByRole("button", { name: "Run daily-etl now" }));
    expect(b.called("POST", `/pipelines/${etl.id}/run`)).toHaveLength(1);
    await waitFor(() => expect(window.location.pathname).toMatch(/^\/pipeline\/runs\/run-/));
  });

  it("can't run a pipeline that has no saved version, and says why", async () => {
    seed();
    renderApp(b);
    const btn = await screen.findByRole("button", { name: "Run adhoc now" });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("title", expect.stringMatching(/Save a version first/));
  });

  it("shows a refused run inline instead of navigating", async () => {
    const { etl } = seed();
    b.failNext.set(`POST /pipelines/${etl.id}/run`, { status: 409, error: "this pipeline already has an active run" });
    renderApp(b);
    await userEvent.click(await screen.findByRole("button", { name: "Run daily-etl now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already has an active run");
    expect(window.location.pathname).toBe("/pipeline/pipelines");
  });

  it("deletes only after an inline confirmation", async () => {
    const { adhoc } = seed();
    renderApp(b);
    await userEvent.click(await screen.findByRole("button", { name: "More actions for adhoc" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Delete pipeline…" }));
    expect(b.called("DELETE", "/pipelines")).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "Delete pipeline" }));
    await waitFor(() => expect(b.called("DELETE", `/pipelines/${adhoc.id}`)).toHaveLength(1));
    await waitFor(() => expect(screen.queryByRole("link", { name: "adhoc" })).not.toBeInTheDocument());
  });

  it("creates a pipeline and opens it", async () => {
    renderApp(b);
    await userEvent.click((await screen.findAllByRole("button", { name: "+ New pipeline" }))[0]);
    await userEvent.type(screen.getByLabelText(/Name/), "nightly");
    await userEvent.click(screen.getByRole("button", { name: "Create and open" }));
    await waitFor(() => expect(window.location.pathname).toBe(`/pipeline/pipelines/${b.pipelines[0].id}`));
    expect(b.called("POST", "/pipelines")[0].body).toEqual({ name: "nightly", description: "" });
  });

  it("searches server-side", async () => {
    seed();
    renderApp(b);
    await screen.findByRole("link", { name: "daily-etl" });
    await userEvent.type(screen.getByRole("searchbox", { name: "Search pipelines" }), "churn");
    await waitFor(() => expect(screen.queryByRole("link", { name: "daily-etl" })).not.toBeInTheDocument());
    expect(b.calls.some((c) => c.method === "GET" && c.path.startsWith("/pipelines?") && c.path.includes("q=churn"))).toBe(true);
  });

  it("offers viewers no write controls at all", async () => {
    seed();
    renderApp(b, { role: "viewer" });
    await screen.findByRole("link", { name: "daily-etl" });
    expect(screen.queryByRole("button", { name: /New pipeline/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Run .* now/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /More actions/ })).not.toBeInTheDocument();
  });

  it("has a helpful empty state", async () => {
    renderApp(b, { role: "viewer" });
    expect(await screen.findByText("No pipelines yet")).toBeInTheDocument();
    expect(screen.getByText(/Someone with edit access/)).toBeInTheDocument();
  });
});
