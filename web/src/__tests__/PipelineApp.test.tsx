import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PipelineApp } from "../PipelineApp";
import type { WorkspaceRole } from "../types";
import { ETL, FakeBackend, getToken, task } from "./testUtils";

// The real React Flow canvas needs a browser to lay out (it is checked in a real browser, see the
// README); here it is replaced by a plain list so these tests exercise OUR wiring — what the
// editor hands the canvas, and what it does with the canvas's callbacks.
vi.mock("../components/DagCanvas", () => ({
  DagCanvas: (props: {
    tasks: { key: string; dependsOn: string[]; position: { x: number; y: number } }[];
    selected: string | null;
    onSelect: (k: string | null) => void;
    onChange?: (t: unknown[]) => void;
    errors?: Record<string, string>;
    statuses?: Record<string, string>;
  }) => (
    <div data-testid="canvas" data-editable={props.onChange ? "yes" : "no"}>
      {props.tasks.map((t) => (
        <button
          key={t.key}
          type="button"
          data-testid={`node-${t.key}`}
          data-position={`${t.position.x},${t.position.y}`}
          aria-pressed={props.selected === t.key}
          onClick={() => props.onSelect(t.key)}
        >
          {t.key}
          {t.dependsOn.length ? ` <- ${t.dependsOn.join(",")}` : ""}
          {props.errors?.[t.key] ? ` [ERROR: ${props.errors[t.key]}]` : ""}
          {props.statuses?.[t.key] ? ` [${props.statuses[t.key]}]` : ""}
        </button>
      ))}
    </div>
  ),
}));

let be: FakeBackend;
beforeEach(() => {
  be = new FakeBackend();
  be.install();
  window.history.replaceState({}, "", "/pipeline/pipelines");
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mount(role: WorkspaceRole = "editor", path = "/pipeline/pipelines") {
  window.history.replaceState({}, "", path);
  return render(<PipelineApp workspace="acme" role={role} theme="light" getAccessToken={getToken} />);
}

describe("pipeline list", () => {
  it("shows pipelines and, for an editor, the create and delete controls", async () => {
    be.addPipeline("nightly-etl", ETL());
    mount("editor");
    expect(await screen.findByRole("link", { name: "nightly-etl" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New pipeline" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete nightly-etl" })).toBeInTheDocument();
    expect(screen.getByText("v1")).toBeInTheDocument();
  });

  it("gives a viewer no way to create or delete", async () => {
    be.addPipeline("nightly-etl", ETL());
    mount("viewer");
    await screen.findByRole("link", { name: "nightly-etl" });
    expect(screen.queryByRole("button", { name: "New pipeline" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Delete/ })).not.toBeInTheDocument();
  });

  it("creates a pipeline and lands in its builder", async () => {
    const user = userEvent.setup();
    mount("editor");
    await user.click(await screen.findByRole("button", { name: "New pipeline" }));
    await user.type(screen.getByLabelText(/Name/), "fresh");
    await user.click(screen.getByRole("button", { name: /Create and open the builder/ }));
    await waitFor(() => expect(window.location.pathname).toBe("/pipeline/pipelines/p1"));
    expect(await screen.findByText("This pipeline is empty")).toBeInTheDocument();
    expect(be.called("POST", "/pipelines")[0].body).toEqual({ name: "fresh", description: "" });
  });

  it("shows a duplicate-name error next to the name field", async () => {
    const user = userEvent.setup();
    be.failNext.set("POST /pipelines", { status: 409, error: "a pipeline with that name already exists in this workspace", field: "name" });
    mount("editor");
    await user.click(await screen.findByRole("button", { name: "New pipeline" }));
    await user.type(screen.getByLabelText(/Name/), "dup");
    await user.click(screen.getByRole("button", { name: /Create and open/ }));
    expect(await screen.findByText(/already exists in this workspace/)).toBeInTheDocument();
    expect(window.location.pathname).toBe("/pipeline/pipelines");
  });

  it("every request carries the workspace and bearer token from the shell's props", async () => {
    mount("editor");
    await screen.findByText(/No pipelines yet/);
    for (const c of be.calls) {
      expect(c.headers.get("X-Workspace")).toBe("acme");
      expect(c.headers.get("Authorization")).toBe("Bearer tok-test");
    }
    expect(be.calls.length).toBeGreaterThan(0);
  });
});

describe("builder", () => {
  it("loads the latest version onto the canvas", async () => {
    be.addPipeline("etl", ETL());
    mount("editor", "/pipeline/pipelines/p1");
    expect(await screen.findByTestId("node-extract")).toBeInTheDocument();
    expect(screen.getByTestId("node-clean")).toHaveTextContent("<- extract");
    expect(screen.getByTestId("node-load")).toHaveTextContent("<- clean");
  });

  it("a pipeline whose tasks all default to {0,0} (e.g. built directly against the API) is laid out on open, not left stacked and looking blank", async () => {
    // ETL()'s tasks (like every task() the helper builds) all default to position {0,0} — this
    // is exactly the shape a raw API caller (booth-e2e's smoke tests included) produces.
    be.addPipeline("etl", ETL());
    mount("editor", "/pipeline/pipelines/p1");
    await screen.findByTestId("node-extract");
    const positions = ["extract", "clean", "load"].map((k) => screen.getByTestId(`node-${k}`).dataset.position);
    expect(new Set(positions).size).toBe(3); // three distinct spots, not one shared stack
    // and — the important part — this must not look like an unsaved edit the moment it opens
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("a pipeline whose tasks already have distinct positions is left exactly as saved", async () => {
    be.addPipeline("placed", [
      task("a", "source", [], { position: { x: 40, y: 10 } }),
      task("b", "sink", ["a"], { position: { x: 340, y: 90 } }),
    ]);
    mount("editor", "/pipeline/pipelines/p1");
    await screen.findByTestId("node-a");
    expect(screen.getByTestId("node-a").dataset.position).toBe("40,10");
    expect(screen.getByTestId("node-b").dataset.position).toBe("340,90");
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("an empty pipeline offers the source -> transform -> sink starter, which is then valid to save", async () => {
    const user = userEvent.setup();
    be.addPipeline("blank");
    mount("editor", "/pipeline/pipelines/p1");
    await user.click(await screen.findByRole("button", { name: /Start with source/ }));
    expect(screen.getByTestId("node-source_1")).toBeInTheDocument();
    expect(screen.getByTestId("node-transform_1")).toHaveTextContent("<- source_1");
    expect(screen.getByTestId("node-sink_1")).toHaveTextContent("<- transform_1");
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save as new version" }));
    await waitFor(() => expect(be.called("POST", "/pipelines/p1/versions")).toHaveLength(1));
    const sent = be.called("POST", "/pipelines/p1/versions")[0].body as { spec: { tasks: { key: string; dependsOn: string[]; runner: string }[] } };
    expect(sent.spec.tasks.map((t) => [t.key, t.dependsOn, t.runner])).toEqual([
      ["source_1", [], "base"], // the base runner is what a new task targets (ADR 0006)
      ["transform_1", ["source_1"], "base"],
      ["sink_1", ["transform_1"], "base"],
    ]);
    expect(await screen.findByText(/Saved as version 1/)).toBeInTheDocument();
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("adds tasks, and selecting one opens its settings", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", ETL());
    mount("editor", "/pipeline/pipelines/p1");
    await screen.findByTestId("node-extract");
    await user.click(screen.getByRole("button", { name: "+ Add task" }));
    expect(screen.getByTestId("node-task_1")).toBeInTheDocument();
    expect(screen.getByRole("form", { name: /Configure task task_1/ })).toBeInTheDocument();
    await user.click(screen.getByTestId("node-clean"));
    expect(screen.getByRole("form", { name: /Configure task clean/ })).toBeInTheDocument();
  });

  it("surfaces a server validation error on the offending node and in a banner, and does not lose the draft", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", ETL());
    be.failNext.set("POST /pipelines/p1/versions", { status: 422, error: "'clean' depends on 'ghost', which is not a task in this pipeline", field: "tasks[1].dependsOn" });
    mount("editor", "/pipeline/pipelines/p1");
    await screen.findByTestId("node-extract");
    await user.click(screen.getByRole("button", { name: "+ Add task" })); // make it dirty so save is enabled
    await user.click(screen.getByRole("button", { name: "Save as new version" }));
    // shown in the page banner AND on the offending task's own panel (it is auto-selected)
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.length).toBeGreaterThanOrEqual(1);
    expect(alerts.every((a) => a.textContent?.includes("depends on 'ghost'"))).toBe(true);
    expect(screen.getByTestId("node-clean")).toHaveTextContent("[ERROR:");
    expect(screen.getByTestId("node-task_1")).toBeInTheDocument(); // the draft survived
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
  });

  it("live validation from the server is shown while drawing, without saving anything", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", ETL());
    mount("editor", "/pipeline/pipelines/p1");
    await screen.findByTestId("node-extract");
    const orig = globalThis.fetch as unknown as (u: string, i?: RequestInit) => Promise<Response>;
    vi.stubGlobal("fetch", (u: string, i?: RequestInit) =>
      u.endsWith("/pipelines/validate")
        ? Promise.resolve(new Response(JSON.stringify({ valid: false, error: "sink 'sink_1' needs at least one upstream task", field: "tasks[3].dependsOn" }), { status: 200, headers: { "Content-Type": "application/json" } }))
        : orig(u, i),
    );
    await user.click(screen.getByRole("button", { name: "+ Add task" }));
    expect(await screen.findByText(/Not ready to save: sink 'sink_1' needs at least one upstream/, {}, { timeout: 3000 })).toBeInTheDocument();
    expect(be.called("POST", "/pipelines/p1/versions")).toHaveLength(0);
  });

  it("a viewer sees the graph but every editing control is gone", async () => {
    be.addPipeline("etl", ETL());
    mount("viewer", "/pipeline/pipelines/p1");
    await screen.findByTestId("node-extract");
    expect(screen.getByTestId("canvas")).toHaveAttribute("data-editable", "no");
    for (const name of ["+ Add task", "Save as new version", "Tidy layout"]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
    expect(screen.getByText(/read-only, so the builder is view-only/)).toBeInTheDocument();
    await userEvent.setup().click(screen.getByTestId("node-clean"));
    expect(screen.getByRole("form", { name: /Configure task clean/ }).querySelector("fieldset")).toBeDisabled();
  });

  it("viewing an old version says so and saving would create the next version", async () => {
    be.addPipeline("etl", ETL());
    be.saveVersion("p1", [task("only", "source")], "trimmed");
    mount("editor", "/pipeline/pipelines/p1/v/1");
    expect(await screen.findByText(/You are viewing version 1 of 2/)).toBeInTheDocument();
    expect(screen.getByText(/Saving creates version 3/)).toBeInTheDocument();
    expect(screen.getByTestId("node-extract")).toBeInTheDocument();
  });
});

describe("task settings", () => {
  async function open(role: WorkspaceRole = "editor") {
    const user = userEvent.setup();
    be.addPipeline("etl", ETL());
    mount(role, "/pipeline/pipelines/p1");
    await user.click(await screen.findByTestId("node-clean"));
    return { user, form: screen.getByRole("form", { name: /Configure task clean/ }) };
  }

  it("edits the inline code of the selected task", async () => {
    const { user } = await open();
    const box = screen.getByLabelText("Python source");
    await user.clear(box);
    await user.type(box, "print(1)");
    expect(screen.getByLabelText("Python source")).toHaveValue("print(1)");
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
  });

  it("lists Spark as unavailable, with why, and cannot select it — base is the default", async () => {
    await open();
    const runs = screen.getByLabelText("Runs on") as HTMLSelectElement;
    expect(runs).toHaveValue("base");
    expect(within(runs).getByRole("option", { name: "Spark (unavailable)" })).toBeDisabled();
    expect(screen.getByText(/booth-spark does not yet define a compute-submission interface/)).toBeInTheDocument();
  });

  it("sets a per-task retry override and can hand back to the job default", async () => {
    const { user } = await open();
    const max = screen.getByLabelText("Max retries");
    await user.clear(max);
    await user.type(max, "3");
    await user.selectOptions(screen.getByLabelText("Backoff"), "exponential");
    expect(screen.getByLabelText("Max retries")).toHaveValue(3);
    await user.click(screen.getByRole("button", { name: "Use the job's default" }));
    expect(screen.getByLabelText("Max retries")).toHaveValue(0);
  });

  it("kind is optional, untaggable, and never gates what a task can depend on (ADR 0062)", async () => {
    const { user } = await open(); // 'clean', tagged "transform", already depends on 'extract' (a "source")
    const kindSelect = screen.getByLabelText("Type") as HTMLSelectElement;
    expect(kindSelect).toHaveValue("transform");
    expect(kindSelect).not.toBeRequired();
    await user.selectOptions(kindSelect, "— untagged —");
    expect(kindSelect).toHaveValue("");
    // still wired to 'extract' — untagging changed nothing about the DAG
    expect(screen.getByTestId("node-clean")).toHaveTextContent("<- extract");
    await user.selectOptions(kindSelect, "Source — where data comes from");
    // now tagged "source" while still depending on something — no longer a contradiction
    expect(screen.getByTestId("node-clean")).toHaveTextContent("<- extract");
  });

  it("wires dependencies with checkboxes — only tasks that could legally feed this one are offered", async () => {
    const { user } = await open();
    const deps = screen.getByRole("region", { name: "Dependencies" });
    // 'load' already depends on 'clean' (ETL), so offering it here would close a cycle — kind plays
    // no part in this exclusion (ADR 0062); 'extract' is already wired; nothing else is legal
    expect(within(deps).queryByText("load")).not.toBeInTheDocument();
    expect(within(deps).getByRole("checkbox")).toBeChecked();
    await user.click(within(deps).getByRole("checkbox"));
    expect(screen.getByTestId("node-clean").textContent).not.toContain("<-");
  });

  it("renames a key everywhere it is used", async () => {
    const { user } = await open();
    const key = screen.getByLabelText(/^Key/);
    await user.clear(key);
    await user.type(key, "tidy{Enter}");
    expect(screen.getByTestId("node-tidy")).toHaveTextContent("<- extract");
    expect(screen.getByTestId("node-load")).toHaveTextContent("<- tidy");
  });

  it("refuses an invalid or colliding key with an explanation", async () => {
    const { user } = await open();
    const key = screen.getByLabelText(/^Key/);
    await user.clear(key);
    await user.type(key, "Bad Key");
    expect(screen.getByText(/lowercase letters, digits and underscores/)).toBeInTheDocument();
    await user.clear(key);
    await user.type(key, "load");
    expect(screen.getByText(/Another task already uses this key/)).toBeInTheDocument();
  });

  it("params must be a JSON object; good JSON is applied, bad JSON is flagged not applied", async () => {
    const { user } = await open();
    const box = screen.getByLabelText(/Parameters/);
    await user.clear(box);
    await user.click(box);
    await user.paste('{"n": 5}');
    expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // valid JSON: no error
    await user.clear(box);
    await user.paste("[1,2]");
    expect(await screen.findByText(/params must be a JSON object/)).toBeInTheDocument();
  });

  it("deleting a task removes it and everything wired to it", async () => {
    const { user } = await open();
    await user.click(screen.getByRole("button", { name: "Delete task" }));
    expect(screen.queryByTestId("node-clean")).not.toBeInTheDocument();
    expect(screen.getByTestId("node-load").textContent).not.toContain("<-");
  });
});

describe("code catalog picker", () => {
  async function pickCatalog() {
    const user = userEvent.setup();
    be.addPipeline("etl", ETL());
    mount("editor", "/pipeline/pipelines/p1");
    await user.click(await screen.findByTestId("node-clean"));
    await user.click(screen.getByLabelText("From the code catalog"));
    return user;
  }

  it("references a catalog entry as {entryId, version} — the narrow shape of ADR 0010", async () => {
    const user = await pickCatalog();
    await user.click(await screen.findByRole("button", { name: "Use loader" }));
    const save = screen.getByRole("button", { name: "Save as new version" });
    await user.click(save);
    await waitFor(() => expect(be.called("POST", "/pipelines/p1/versions")).toHaveLength(1));
    const sent = be.called("POST", "/pipelines/p1/versions")[0].body as { spec: { tasks: { key: string; code: Record<string, unknown> }[] } };
    const code = sent.spec.tasks.find((t) => t.key === "clean")!.code;
    expect(code).toMatchObject({ type: "catalog", entryId: "e1", version: "latest" }); // the server pins "latest" on save
  });

  it("offers the entry's versions for pinning", async () => {
    const user = await pickCatalog();
    await user.click(await screen.findByRole("button", { name: "Use loader" }));
    const sel = await screen.findByLabelText("Version", { selector: "#catalog-version" });
    await user.selectOptions(sel, "1.0.0");
    expect(sel).toHaveValue("1.0.0");
  });

  it("a missing catalog is a warning beside the picker, never a broken builder — inline code still works", async () => {
    be.catalogDown = true;
    await pickCatalog();
    expect(await screen.findByText(/The code catalog could not be reached/)).toBeInTheDocument();
    expect(screen.getByText(/Inline code needs no catalog/)).toBeInTheDocument();
    // the rest of the builder is untouched
    expect(screen.getByRole("button", { name: "+ Add task" })).toBeEnabled();
    await userEvent.setup().click(screen.getByLabelText("Write code here"));
    expect(screen.getByLabelText("Python source")).toBeInTheDocument();
  });
});

describe("jobs", () => {
  it("creates a scheduled job with a retry default from a saved pipeline", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", ETL());
    mount("editor", "/pipeline/jobs/new?pipeline=p1");
    await user.type(await screen.findByLabelText(/^Name/), "nightly");
    await user.click(screen.getByLabelText("Run on a schedule"));
    await user.selectOptions(screen.getByLabelText("Frequency"), "Custom (cron expression)");
    const cron = screen.getByLabelText(/Cron expression/);
    await user.clear(cron);
    await user.type(cron, "0 2 * * *");
    const tz = screen.getByLabelText("Timezone");
    await user.clear(tz);
    await user.type(tz, "America/Toronto");
    await user.click(screen.getByLabelText("Retry failed tasks by default"));
    await user.click(screen.getByRole("button", { name: "Create job" }));
    await waitFor(() => expect(be.called("POST", "/jobs")).toHaveLength(1));
    expect(be.called("POST", "/jobs")[0].body).toEqual({
      name: "nightly",
      pipelineId: "p1",
      pipelineVersion: null, // follows the latest by default
      schedule: { type: "cron", cron: "0 2 * * *", timezone: "America/Toronto", enabled: true },
      retry: { maxRetries: 2, delaySeconds: 30, backoff: "exponential" },
      allowConcurrentRuns: false,
      roleCeiling: "editor", // the default: read and write, never owner
    });
    await waitFor(() => expect(window.location.pathname).toBe("/pipeline/jobs/j1"));
  });

  it("creates a job on a sub-minute interval trigger (ADR 0065)", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", ETL());
    mount("editor", "/pipeline/jobs/new?pipeline=p1");
    await user.type(await screen.findByLabelText(/^Name/), "frequent");
    await user.click(screen.getByLabelText("Run on a schedule"));
    await user.selectOptions(screen.getByLabelText("Frequency"), "Every N seconds");
    expect(screen.queryByLabelText("Timezone")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create job" }));
    await waitFor(() => expect(be.called("POST", "/jobs")).toHaveLength(1));
    expect((be.called("POST", "/jobs")[0].body as { schedule: unknown }).schedule).toEqual({ type: "interval", seconds: 5, enabled: true });
  });

  it("can pin a job to a specific version", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", ETL());
    be.saveVersion("p1", [task("a", "source")]);
    mount("editor", "/pipeline/jobs/new?pipeline=p1");
    await user.type(await screen.findByLabelText(/^Name/), "pinned");
    await user.selectOptions(await screen.findByLabelText("Version"), "1");
    await user.click(screen.getByRole("button", { name: "Create job" }));
    await waitFor(() => expect(be.called("POST", "/jobs")).toHaveLength(1));
    expect((be.called("POST", "/jobs")[0].body as { pipelineVersion: number }).pipelineVersion).toBe(1);
  });

  it("maps a bad-schedule error onto the cron field", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", ETL());
    be.failNext.set("POST /jobs", { status: 422, error: "cron must have exactly 5 fields: minute hour day-of-month month day-of-week", field: "schedule.cron" });
    mount("editor", "/pipeline/jobs/new?pipeline=p1");
    await user.type(await screen.findByLabelText(/^Name/), "bad");
    await user.click(screen.getByLabelText("Run on a schedule"));
    await user.click(screen.getByRole("button", { name: "Create job" }));
    expect(await screen.findByText(/cron must have exactly 5 fields/)).toBeInTheDocument();
    expect(window.location.pathname).toBe("/pipeline/jobs/new");
  });

  it("explains there is nothing to run when no pipeline has been saved yet", async () => {
    be.addPipeline("blank");
    mount("editor", "/pipeline/jobs/new");
    expect(await screen.findByText(/A job needs a pipeline with at least one saved version/)).toBeInTheDocument();
  });

  it("run now starts a run and opens it", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", ETL());
    be.jobs.push({ id: "j1", name: "nightly", pipelineId: "p1", pipelineVersion: null, schedule: null, retry: null, allowConcurrentRuns: false, roleCeiling: "editor", hasOwner: true, createdBy: "e", createdAt: "", updatedAt: "", nextRunAt: null });
    mount("editor", "/pipeline/jobs");
    await user.click(await screen.findByRole("button", { name: "Run nightly now" }));
    await waitFor(() => expect(window.location.pathname).toMatch(/^\/pipeline\/runs\/run-1/));
  });

  it("an overlapping run is refused with the server's explanation", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", ETL());
    be.jobs.push({ id: "j1", name: "nightly", pipelineId: "p1", pipelineVersion: null, schedule: null, retry: null, allowConcurrentRuns: false, roleCeiling: "editor", hasOwner: true, createdBy: "e", createdAt: "", updatedAt: "", nextRunAt: null });
    be.failNext.set("POST /jobs/j1/run", { status: 409, error: "this job already has an active run" });
    mount("editor", "/pipeline/jobs");
    await user.click(await screen.findByRole("button", { name: "Run nightly now" }));
    expect(await screen.findByText(/already has an active run/)).toBeInTheDocument();
    expect(window.location.pathname).toBe("/pipeline/jobs");
  });

  it("a viewer can read jobs but not run or create them", async () => {
    be.addPipeline("etl", ETL());
    be.jobs.push({ id: "j1", name: "nightly", pipelineId: "p1", pipelineVersion: null, schedule: { type: "cron", cron: "0 9 * * 1-5", timezone: "UTC", enabled: true }, retry: null, allowConcurrentRuns: false, roleCeiling: "editor", hasOwner: true, createdBy: "e", createdAt: "", updatedAt: "", nextRunAt: null });
    mount("viewer", "/pipeline/jobs");
    expect(await screen.findByText("weekdays at 09:00 (UTC)")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Run nightly now/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "New job" })).not.toBeInTheDocument();
  });
});
