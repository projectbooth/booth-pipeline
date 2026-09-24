import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PipelineApp } from "../PipelineApp";
import { FakeBackend, getToken } from "./testUtils";

vi.mock("../components/DagCanvas", () => ({
  DagCanvas: (props: { refs: { key: string }[]; onSelect: (k: string | null) => void }) => (
    <div data-testid="canvas">
      {props.refs.map((r) => (
        <button key={r.key} type="button" data-testid={`node-${r.key}`} onClick={() => props.onSelect(r.key)}>
          {r.key}
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

function mount(path: string, role: "owner" | "editor" | "viewer" = "editor") {
  window.history.replaceState({}, "", path);
  return render(<PipelineApp workspace="acme" role={role} theme="light" getAccessToken={getToken} />);
}

describe("platform access on a task", () => {
  it("is off by default and is a per-task opt-in that explains itself, saved as a new task version", async () => {
    const user = userEvent.setup();
    const refs = [be.ref("extract"), be.ref("clean", ["extract"]), be.ref("load", ["clean"])];
    const p = be.addPipeline("etl", refs);
    mount(`/pipeline/pipelines/${p.id}`);
    await user.click(await screen.findByTestId("node-clean"));
    const box = await screen.findByRole("checkbox", { name: /Platform access/ });
    expect(box).not.toBeChecked();
    expect(screen.getByText(/never asks for an identity, so it can't be blocked by one/)).toBeInTheDocument();
    expect(screen.getByText(/hasn't signed in to the platform recently/)).toBeInTheDocument();
    await user.click(box);
    await user.click(screen.getByRole("button", { name: "Save as new task version" }));
    const cleanTaskId = refs[1].taskId;
    await waitFor(() => expect(be.called("POST", `/tasks/${cleanTaskId}/versions`)).toHaveLength(1));
    const sent = be.called("POST", `/tasks/${cleanTaskId}/versions`)[0].body as { config: { platformAccess: boolean } };
    expect(sent.config.platformAccess).toBe(true);
    // saving a task's config never touches the pipeline itself
    expect(be.called("POST", `/pipelines/${p.id}/versions`)).toHaveLength(0);
  });

  it("only that task is changed, and a viewer cannot toggle it", async () => {
    const refs = [be.ref("extract"), be.ref("clean", ["extract"]), be.ref("load", ["clean"])];
    const p = be.addPipeline("etl", refs);
    mount(`/pipeline/pipelines/${p.id}`, "viewer");
    await userEvent.setup().click(await screen.findByTestId("node-clean"));
    expect(await screen.findByRole("checkbox", { name: /Platform access/ })).toBeDisabled();
  });
});

describe("a pipeline's role ceiling", () => {
  async function openSchedule(user: ReturnType<typeof userEvent.setup>, path: string) {
    mount(path);
    await user.click(await screen.findByRole("button", { name: "Schedule" }));
    return (await screen.findByLabelText(/Role ceiling for platform access/)) as HTMLSelectElement;
  }

  it("defaults to editor, offers viewer, and there is no owner option", async () => {
    const user = userEvent.setup();
    const p = be.addPipeline("etl", [be.ref("extract")]);
    const sel = await openSchedule(user, `/pipeline/pipelines/${p.id}`);
    expect(sel).toHaveValue("editor");
    expect([...sel.options].map((o) => o.value)).toEqual(["editor", "viewer"]);
    await user.selectOptions(sel, "viewer");
    await user.click(screen.getByRole("button", { name: "Save schedule" }));
    await waitFor(() => expect(be.called("PUT", `/pipelines/${p.id}/schedule`)).toHaveLength(1));
    expect((be.called("PUT", `/pipelines/${p.id}/schedule`)[0].body as { roleCeiling: string }).roleCeiling).toBe("viewer");
  });

  it("an existing pipeline shows its ceiling and saves a change to it", async () => {
    const user = userEvent.setup();
    const p = be.addPipeline("etl", [be.ref("extract")]);
    p.roleCeiling = "viewer";
    const sel = await openSchedule(user, `/pipeline/pipelines/${p.id}`);
    expect(sel).toHaveValue("viewer");
    await user.selectOptions(sel, "editor");
    await user.click(screen.getByRole("button", { name: "Save schedule" }));
    await waitFor(() => expect(be.called("PUT", `/pipelines/${p.id}/schedule`)).toHaveLength(1));
    expect((be.called("PUT", `/pipelines/${p.id}/schedule`)[0].body as { roleCeiling: string }).roleCeiling).toBe("editor");
  });

  it("warns that a pipeline from before workload identity has no owner, once a schedule is set", async () => {
    const user = userEvent.setup();
    const p = be.addPipeline("etl", [be.ref("extract")]);
    p.hasOwner = false;
    mount(`/pipeline/pipelines/${p.id}`);
    await user.click(await screen.findByRole("button", { name: "Schedule" }));
    await user.click(await screen.findByRole("checkbox", { name: "Run on a schedule" }));
    expect(await screen.findByText(/predates workload identity and has no owner/)).toBeInTheDocument();
    expect(screen.getByText(/Saving a schedule here makes you its owner/)).toBeInTheDocument();
  });

  it("a pipeline that has an owner shows no such warning", async () => {
    const user = userEvent.setup();
    const p = be.addPipeline("etl", [be.ref("extract")]);
    p.hasOwner = true;
    await openSchedule(user, `/pipeline/pipelines/${p.id}`);
    expect(screen.queryByText(/predates workload identity/)).not.toBeInTheDocument();
  });
});

describe("a run refused platform access", () => {
  it("shows the reason on the run and on the task, plainly", async () => {
    const p = be.addPipeline("etl", [be.ref("extract"), be.ref("clean", ["extract"]), be.ref("load", ["clean"])]);
    const run = be.newRun(p.id);
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
