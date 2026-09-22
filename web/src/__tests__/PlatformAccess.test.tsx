import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PipelineApp } from "../PipelineApp";
import { newTask } from "../graph";
import { ETL, FakeBackend, getToken } from "./testUtils";

vi.mock("../components/DagCanvas", () => ({
  DagCanvas: (props: { tasks: { key: string }[]; onSelect: (k: string | null) => void }) => (
    <div data-testid="canvas">
      {props.tasks.map((t) => (
        <button key={t.key} type="button" data-testid={`node-${t.key}`} onClick={() => props.onSelect(t.key)}>
          {t.key}
        </button>
      ))}
    </div>
  ),
}));

let be: FakeBackend;
beforeEach(() => {
  be = new FakeBackend();
  be.install();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mount(path: string) {
  window.history.replaceState({}, "", path);
  return render(<PipelineApp workspace="acme" role="editor" theme="light" getAccessToken={getToken} />);
}

const job = (over: Record<string, unknown> = {}) => ({
  id: "j1",
  name: "nightly",
  pipelineId: "p1",
  pipelineVersion: null,
  schedule: null,
  retry: null,
  allowConcurrentRuns: false,
  roleCeiling: "editor" as const,
  hasOwner: true,
  createdBy: "e",
  createdAt: "",
  updatedAt: "",
  nextRunAt: null,
  ...over,
});

describe("platform access on a task", () => {
  it("is off by default: a new task asks for no identity", () => {
    expect(newTask("source", []).platformAccess).toBe(false);
  });

  it("is a per-task opt-in that explains itself and is saved in the spec", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", ETL());
    mount("/pipeline/pipelines/p1");
    await user.click(await screen.findByTestId("node-clean"));
    const box = screen.getByRole("checkbox", { name: /Platform access/ });
    expect(box).not.toBeChecked();
    expect(screen.getByText(/never asks for an identity, so it can't be blocked by one/)).toBeInTheDocument();
    expect(screen.getByText(/hasn't signed in to the platform recently/)).toBeInTheDocument();
    await user.click(box);
    await user.click(screen.getByRole("button", { name: "Save as new version" }));
    await waitFor(() => expect(be.called("POST", "/pipelines/p1/versions")).toHaveLength(1));
    const sent = be.called("POST", "/pipelines/p1/versions")[0].body as { spec: { tasks: { key: string; platformAccess: boolean }[] } };
    expect(Object.fromEntries(sent.spec.tasks.map((t) => [t.key, t.platformAccess]))).toEqual({ extract: false, clean: true, load: false });
  });

  it("only that task is changed, and a viewer cannot toggle it", async () => {
    be.addPipeline("etl", ETL());
    window.history.replaceState({}, "", "/pipeline/pipelines/p1");
    render(<PipelineApp workspace="acme" role="viewer" theme="light" getAccessToken={getToken} />);
    await userEvent.setup().click(await screen.findByTestId("node-clean"));
    expect(screen.getByRole("checkbox", { name: /Platform access/ })).toBeDisabled();
  });
});

describe("the job's role ceiling", () => {
  it("defaults to editor, offers viewer, and there is no owner option", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", ETL());
    mount("/pipeline/jobs/new?pipeline=p1");
    const sel = (await screen.findByLabelText(/Role ceiling for platform access/)) as HTMLSelectElement;
    expect(sel).toHaveValue("editor");
    expect([...sel.options].map((o) => o.value)).toEqual(["editor", "viewer"]);
    await user.type(screen.getByLabelText(/^Name/), "read-only");
    await user.selectOptions(sel, "viewer");
    await user.click(screen.getByRole("button", { name: "Create job" }));
    await waitFor(() => expect(be.called("POST", "/jobs")).toHaveLength(1));
    expect((be.called("POST", "/jobs")[0].body as { roleCeiling: string }).roleCeiling).toBe("viewer");
  });

  it("an existing job shows its ceiling and saves a change to it", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", ETL());
    be.jobs.push(job({ roleCeiling: "viewer" }) as never);
    mount("/pipeline/jobs/j1");
    const sel = await screen.findByLabelText(/Role ceiling for platform access/);
    expect(sel).toHaveValue("viewer");
    await user.selectOptions(sel, "editor");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(be.called("PUT", "/jobs/j1")).toHaveLength(1));
    expect((be.called("PUT", "/jobs/j1")[0].body as { roleCeiling: string }).roleCeiling).toBe("editor");
  });

  it("warns that a job from before workload identity has no owner, and says how to fix it", async () => {
    be.addPipeline("etl", ETL());
    be.jobs.push(job({ hasOwner: false }) as never);
    mount("/pipeline/jobs/j1");
    expect(await screen.findByText(/predates workload identity and has no owner/)).toBeInTheDocument();
    expect(screen.getByText(/Saving it makes you its owner/)).toBeInTheDocument();
  });

  it("a job that has an owner shows no such warning", async () => {
    be.addPipeline("etl", ETL());
    be.jobs.push(job() as never);
    mount("/pipeline/jobs/j1");
    await screen.findByLabelText(/Role ceiling for platform access/);
    expect(screen.queryByText(/predates workload identity/)).not.toBeInTheDocument();
  });
});

describe("a run refused platform access", () => {
  it("shows the reason on the run and on the task, plainly", async () => {
    be.addPipeline("etl", ETL());
    be.jobs.push(job() as never);
    const run = be.newRun("j1");
    const reason = "not given platform access: the owner of this run hasn't signed in to the platform recently enough to authorize its access to storage and the catalog";
    run.status = "failed";
    run.finishedAt = "2026-09-21T12:00:01+00:00";
    run.error = `task(s) failed: clean: ${reason}`;
    run.tasks = [
      { taskKey: "extract", status: "succeeded", attempts: 1, startedAt: run.startedAt, finishedAt: run.finishedAt, error: null },
      { taskKey: "clean", status: "failed", attempts: 1, startedAt: run.startedAt, finishedAt: run.finishedAt, error: reason },
      { taskKey: "load", status: "skipped", attempts: 0, startedAt: null, finishedAt: null, error: null },
    ];
    mount(`/pipeline/runs/${run.id}`);
    const shown = await screen.findAllByText(/hasn't signed in to the platform recently enough/);
    expect(shown.length).toBeGreaterThanOrEqual(2); // in the run banner AND the task table
  });
});
