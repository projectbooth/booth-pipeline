import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PipelineApp } from "../PipelineApp";
import { formatTime } from "../format";
import type { WorkspaceRole } from "../types";
import { FakeBackend, getToken } from "./testUtils";

// The real React Flow canvas needs a browser to lay out (it is checked in a real browser, see the
// README); here it is replaced by a plain list so these tests exercise OUR wiring — what the
// editor hands the canvas, and what it does with the canvas's callbacks.
vi.mock("../components/DagCanvas", () => ({
  DagCanvas: (props: {
    refs: { key: string; dependsOn: string[]; position: { x: number; y: number } }[];
    selected: string | null;
    onSelect: (k: string | null) => void;
    onChange?: (r: unknown[]) => void;
    errors?: Record<string, string>;
    statuses?: Record<string, string>;
  }) => (
    <div data-testid="canvas" data-editable={props.onChange ? "yes" : "no"}>
      {props.refs.map((r) => (
        <button
          key={r.key}
          type="button"
          data-testid={`node-${r.key}`}
          data-position={`${r.position.x},${r.position.y}`}
          aria-pressed={props.selected === r.key}
          onClick={() => props.onSelect(r.key)}
        >
          {r.key}
          {r.dependsOn.length ? ` <- ${r.dependsOn.join(",")}` : ""}
          {props.errors?.[r.key] ? ` [ERROR: ${props.errors[r.key]}]` : ""}
          {props.statuses?.[r.key] ? ` [${props.statuses[r.key]}]` : ""}
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

function etl() {
  return [be.ref("extract"), be.ref("clean", ["extract"]), be.ref("load", ["clean"])];
}

describe("pipeline list", () => {
  it("shows pipelines and, for an editor, the create and delete controls", async () => {
    be.addPipeline("nightly-etl", etl());
    mount("editor");
    expect(await screen.findByRole("link", { name: "nightly-etl" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New pipeline" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete nightly-etl" })).toBeInTheDocument();
    expect(screen.getByText("v1")).toBeInTheDocument();
  });

  it("gives a viewer no way to create or delete", async () => {
    be.addPipeline("nightly-etl", etl());
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

  it("run now starts a run and opens it", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", etl());
    mount("editor");
    await user.click(await screen.findByRole("button", { name: "Run etl now" }));
    await waitFor(() => expect(window.location.pathname).toMatch(/^\/pipeline\/runs\/run-1/));
  });

  it("an overlapping run is refused with the server's explanation", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", etl());
    be.failNext.set("POST /pipelines/p1/run", { status: 409, error: "this pipeline already has an active run" });
    mount("editor");
    await user.click(await screen.findByRole("button", { name: "Run etl now" }));
    expect(await screen.findByText(/already has an active run/)).toBeInTheDocument();
    expect(window.location.pathname).toBe("/pipeline/pipelines");
  });

  it("a viewer can see a pipeline's schedule but not run it", async () => {
    const p = be.addPipeline("etl", etl());
    p.schedule = { type: "cron", cron: "0 9 * * 1-5", timezone: "UTC", enabled: true };
    p.nextRunAt = "2026-09-22T09:00:00+00:00";
    mount("viewer");
    expect(await screen.findByText("weekdays at 09:00 (UTC)")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Run etl now/ })).not.toBeInTheDocument();
  });
});

describe("builder", () => {
  it("exports the current version as a downloadable YAML file", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", etl());
    mount("editor", "/pipeline/pipelines/p1");
    await screen.findByTestId("node-extract");

    const createObjectURL = vi.fn<(blob: Blob) => string>(() => "blob:mock-url");
    const revokeObjectURL = vi.fn();
    URL.createObjectURL = createObjectURL;
    URL.revokeObjectURL = revokeObjectURL;
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    await user.click(screen.getByRole("button", { name: "Export as YAML" }));

    await waitFor(() => expect(be.called("GET", "/pipelines/p1/versions/1/export")).toHaveLength(1));
    expect(createObjectURL).toHaveBeenCalled();
    const blob = createObjectURL.mock.calls[0][0] as Blob;
    expect(blob.type).toBe("application/yaml");
    expect(clickSpy).toHaveBeenCalled();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock-url");

    clickSpy.mockRestore();
  });

  it("loads the latest version onto the canvas", async () => {
    be.addPipeline("etl", etl());
    mount("editor", "/pipeline/pipelines/p1");
    expect(await screen.findByTestId("node-extract")).toBeInTheDocument();
    expect(screen.getByTestId("node-clean")).toHaveTextContent("<- extract");
    expect(screen.getByTestId("node-load")).toHaveTextContent("<- clean");
  });

  it("a pipeline whose references all default to {0,0} (e.g. built directly against the API) is laid out on open, not left stacked and looking blank", async () => {
    // be.ref()'s references, like the old task() helper's tasks, all default to position {0,0} —
    // exactly the shape a raw API caller (booth-e2e's smoke tests included) produces.
    be.addPipeline("etl", etl());
    mount("editor", "/pipeline/pipelines/p1");
    await screen.findByTestId("node-extract");
    const positions = ["extract", "clean", "load"].map((k) => screen.getByTestId(`node-${k}`).dataset.position);
    expect(new Set(positions).size).toBe(3); // three distinct spots, not one shared stack
    // and — the important part — this must not look like an unsaved edit the moment it opens
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("a pipeline whose references already have distinct positions is left exactly as saved", async () => {
    be.addPipeline("placed", [
      { ...be.ref("a"), position: { x: 40, y: 10 } },
      { ...be.ref("b", ["a"]), position: { x: 340, y: 90 } },
    ]);
    mount("editor", "/pipeline/pipelines/p1");
    await screen.findByTestId("node-a");
    expect(screen.getByTestId("node-a").dataset.position).toBe("40,10");
    expect(screen.getByTestId("node-b").dataset.position).toBe("340,90");
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  // "+ Add task" opens a picker (search existing tasks, or create a new one) rather than always
  // silently creating one — clicks the first "+ Add task" match (an empty pipeline offers it twice:
  // once in the toolbar, once in the empty-state itself), then creates and picks a fresh task named `name`.
  async function addNewTask(user: ReturnType<typeof userEvent.setup>, name: string) {
    const [addButton] = await screen.findAllByRole("button", { name: /\+ Add task/ });
    await user.click(addButton);
    await user.type(await screen.findByPlaceholderText(/Search tasks/), name);
    await user.click(screen.getByRole("button", { name: `+ Create new task "${name}"` }));
  }

  it("adding a task creates a standalone task and references it, and it is valid to save", async () => {
    const user = userEvent.setup();
    be.addPipeline("blank");
    mount("editor", "/pipeline/pipelines/p1");
    await addNewTask(user, "task_1");
    expect(await screen.findByTestId("node-task_1")).toBeInTheDocument();
    expect(be.called("POST", "/tasks").map((c) => (c.body as { name: string }).name)).toEqual(["task_1"]);
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save as new version" }));
    await waitFor(() => expect(be.called("POST", "/pipelines/p1/versions")).toHaveLength(1));
    const sent = be.called("POST", "/pipelines/p1/versions")[0].body as { spec: { tasks: { key: string; dependsOn: string[]; taskId: string }[] } };
    expect(sent.spec.tasks).toEqual([{ key: "task_1", taskId: "t1", taskVersion: "latest", dependsOn: [], position: { x: 0, y: 0 } }]);
    expect(await screen.findByText(/Saved as version 1/)).toBeInTheDocument();
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("adds tasks, and selecting one opens its settings", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", etl());
    mount("editor", "/pipeline/pipelines/p1");
    await screen.findByTestId("node-extract");
    await addNewTask(user, "task_1");
    expect(await screen.findByTestId("node-task_1")).toBeInTheDocument();
    expect(await screen.findByRole("form", { name: /Configure task task_1/ })).toBeInTheDocument();
    await user.click(screen.getByTestId("node-clean"));
    expect(await screen.findByRole("form", { name: /Configure task clean/ })).toBeInTheDocument();
  });

  it("surfaces a server validation error on the offending node and in a banner, and does not lose the draft", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", etl());
    be.failNext.set("POST /pipelines/p1/versions", { status: 422, error: "'clean' depends on 'ghost', which is not a task in this pipeline", field: "tasks[1].dependsOn" });
    mount("editor", "/pipeline/pipelines/p1");
    await screen.findByTestId("node-extract");
    await addNewTask(user, "task_1"); // make it dirty so save is enabled
    await screen.findByTestId("node-task_1");
    await user.click(screen.getByRole("button", { name: "Save as new version" }));
    // shown in the page banner AND on the offending task's own panel (it is auto-selected)
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.length).toBeGreaterThanOrEqual(1);
    expect(alerts.every((a) => a.textContent?.includes("depends on 'ghost'"))).toBe(true);
    expect(screen.getByTestId("node-clean")).toHaveTextContent("[ERROR:");
    expect(screen.getByTestId("node-task_1")).toBeInTheDocument(); // the draft survived
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
  });

  it("+ Add task can wire a node to an existing, already-configured task instead of always creating a new one", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", etl());
    be.addTask("shared-loader", { code: { type: "inline", source: "x=1" }, runner: "base", retry: null, params: {}, timeoutSeconds: 3600, platformAccess: false });
    mount("editor", "/pipeline/pipelines/p1");
    await screen.findByTestId("node-extract");
    const before = be.called("POST", "/tasks").length;
    const [addButton] = await screen.findAllByRole("button", { name: /\+ Add task/ });
    await user.click(addButton);
    await user.type(await screen.findByPlaceholderText(/Search tasks/), "shared-loader");
    await user.click(await screen.findByRole("button", { name: /^shared-loader$/ }));
    expect(be.called("POST", "/tasks")).toHaveLength(before); // no new task was created
    expect(await screen.findByTestId("node-task_1")).toHaveTextContent("task_1");
  });

  it("the add-task picker hides unconfigured tasks by default, badges and reveals them via a toggle", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", etl());
    be.addTask("orphan-task"); // created with no config, e.g. from an earlier "+ Add task" click
    mount("editor", "/pipeline/pipelines/p1");
    await screen.findByTestId("node-extract");
    const [addButton] = await screen.findAllByRole("button", { name: /\+ Add task/ });
    await user.click(addButton);
    await user.type(await screen.findByPlaceholderText(/Search tasks/), "orphan-task");
    expect(await screen.findByText("No configured tasks match — try the toggle below.")).toBeInTheDocument();
    expect(screen.queryByText("orphan-task")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Show 1 unconfigured task too/ }));
    expect(await screen.findByText("orphan-task")).toBeInTheDocument();
    expect(screen.getByText("no saved version yet")).toBeInTheDocument();
  });

  it("the add-task picker treats a task with only a draft (never promoted to a version) as configured", async () => {
    const user = userEvent.setup();
    be.addPipeline("etl", etl());
    const t = be.addTask("draft-only-task"); // no version...
    be.saveTaskDraft(t.id, { code: { type: "inline", source: "x=1" }, runner: "base", retry: null, params: {}, timeoutSeconds: 3600, platformAccess: false }); // ...but a real draft
    mount("editor", "/pipeline/pipelines/p1");
    await screen.findByTestId("node-extract");
    const [addButton] = await screen.findAllByRole("button", { name: /\+ Add task/ });
    await user.click(addButton);
    await user.type(await screen.findByPlaceholderText(/Search tasks/), "draft-only-task");
    expect(await screen.findByText("draft-only-task")).toBeInTheDocument(); // visible without the toggle
    expect(screen.queryByText("no saved version yet")).not.toBeInTheDocument();
  });

  it("a viewer sees the graph but every editing control is gone", async () => {
    be.addPipeline("etl", etl());
    mount("viewer", "/pipeline/pipelines/p1");
    await screen.findByTestId("node-extract");
    expect(screen.getByTestId("canvas")).toHaveAttribute("data-editable", "no");
    for (const name of [/\+ Add task/, "Save as new version", "Tidy layout"]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
    expect(screen.getByText(/read-only, so the builder is view-only/)).toBeInTheDocument();
    await userEvent.setup().click(screen.getByTestId("node-clean"));
    const panel = await screen.findByRole("form", { name: /Configure task clean/ });
    expect(panel.querySelector("fieldset")).toBeDisabled();
  });

  it("viewing an old version says so and saving would create the next version", async () => {
    be.addPipeline("etl", etl());
    be.saveVersion("p1", [be.ref("only")], "trimmed");
    mount("editor", "/pipeline/pipelines/p1/v/1");
    expect(await screen.findByText(/You are viewing version 1 of 2/)).toBeInTheDocument();
    expect(screen.getByText(/Saving creates version 3/)).toBeInTheDocument();
    expect(screen.getByTestId("node-extract")).toBeInTheDocument();
  });

  it("shows when each version was saved in the version switcher", async () => {
    const p = be.addPipeline("etl", etl());
    const version = be.versions.get(p.id)![0];
    mount("editor", "/pipeline/pipelines/p1");
    const option = (await screen.findByRole("option", { name: /^v1/ })) as HTMLOptionElement;
    expect(option.text).toContain(formatTime(version.createdAt));
  });

  // ---- ADR 0073: plain "Save" writes a mutable draft; "Save as new version" stays explicit -----

  it("plain Save writes a pipeline draft without creating a version, and stays on the canvas", async () => {
    const user = userEvent.setup();
    be.addPipeline("blank");
    mount("editor", "/pipeline/pipelines/p1");
    await addNewTask(user, "task_1");
    await screen.findByTestId("node-task_1");
    const toolbar = screen.getByRole("toolbar", { name: "Pipeline builder" });
    await user.click(within(toolbar).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(be.called("PUT", "/pipelines/p1/draft")).toHaveLength(1));
    expect(be.called("POST", "/pipelines/p1/versions")).toHaveLength(0); // no version was created
    expect(await screen.findByText(/"Always follow latest" references pick this up immediately/)).toBeInTheDocument();
    expect(screen.getByText("Draft ahead of the latest saved version")).toBeInTheDocument();
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
    // still on the same screen — no navigation/remount, unlike "Save as new version"
    expect(screen.getByTestId("node-task_1")).toBeInTheDocument();
  });

  it("loads the pipeline's draft onto the canvas in preference to its latest saved version", async () => {
    const p = be.addPipeline("etl", [be.ref("only")]);
    be.savePipelineDraft(p.id, [be.ref("only"), be.ref("added-since")]);
    mount("editor", "/pipeline/pipelines/p1");
    expect(await screen.findByTestId("node-added-since")).toBeInTheDocument();
    expect(screen.getByText("Draft ahead of the latest saved version")).toBeInTheDocument();
  });
});

describe("task settings", () => {
  async function open(role: WorkspaceRole = "editor") {
    const user = userEvent.setup();
    be.addPipeline("etl", etl());
    mount(role, "/pipeline/pipelines/p1");
    await user.click(await screen.findByTestId("node-clean"));
    // waits for the referenced task's own config to finish loading (a real fetch through the fake backend)
    await screen.findByLabelText("Runs on");
    return { user };
  }

  it("shows when each version was saved in the reference panel's task-version dropdown", async () => {
    const refs = etl();
    be.addPipeline("etl", refs);
    const version = be.taskVersions.get(refs[1].taskId)![0]; // "clean"
    mount("editor", "/pipeline/pipelines/p1");
    await userEvent.setup().click(await screen.findByTestId("node-clean"));
    const option = (await screen.findByRole("option", { name: /^Pin to v1/ })) as HTMLOptionElement;
    expect(option.text).toContain(formatTime(version.createdAt));
  });

  it("shows an old task's inline code read-only, with no path to edit it, and lets it be replaced via a picker (ADR 0063)", async () => {
    const { user } = await open();
    expect(screen.getByText(/written directly in the builder/)).toBeInTheDocument();
    const box = screen.getByLabelText("Saved source");
    expect(box).toHaveValue("def run(ctx):\n    return 1\n");
    expect(box).toHaveAttribute("readonly");
    await user.click(screen.getByLabelText("From the code catalog"));
    expect(screen.queryByLabelText("Saved source")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Search the code catalog")).toBeInTheDocument();
  });

  it("lists Spark as unavailable, with why, and cannot select it — base is the default", async () => {
    await open();
    const runs = screen.getByLabelText("Runs on") as HTMLSelectElement;
    expect(runs).toHaveValue("base");
    expect(within(runs).getByRole("option", { name: "Spark (unavailable)" })).toBeDisabled();
    expect(screen.getByText(/booth-spark does not yet define a compute-submission interface/)).toBeInTheDocument();
  });

  it("sets a retry policy and saves it as a new task version, independent of the pipeline", async () => {
    const { user } = await open();
    const max = screen.getByLabelText("Max retries");
    await user.clear(max);
    await user.type(max, "3");
    await user.selectOptions(screen.getByLabelText("Backoff"), "exponential");
    expect(screen.getByLabelText("Max retries")).toHaveValue(3);
    await user.click(screen.getByRole("button", { name: "Save as new task version" }));
    await waitFor(() => expect(be.called("POST", "/tasks/t2/versions")).toHaveLength(1));
    const sent = be.called("POST", "/tasks/t2/versions")[0].body as { config: { retry: { maxRetries: number; backoff: string } } };
    expect(sent.config.retry).toEqual({ maxRetries: 3, delaySeconds: 0, backoff: "exponential" });
  });

  it("wires dependencies with checkboxes — only tasks that could legally feed this one are offered", async () => {
    const { user } = await open();
    const deps = screen.getByRole("region", { name: "Dependencies" });
    // 'load' already depends on 'clean' (etl()), so offering it here would close a cycle;
    // 'extract' is already wired; nothing else is legal
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

  it("removing a task from the pipeline drops it and everything wired to it, without deleting the task itself", async () => {
    const { user } = await open();
    await user.click(screen.getByRole("button", { name: "Remove from pipeline" }));
    expect(screen.queryByTestId("node-clean")).not.toBeInTheDocument();
    expect(screen.getByTestId("node-load").textContent).not.toContain("<-");
    expect(be.called("DELETE", "/tasks/t2")).toHaveLength(0); // the standalone task still exists
  });

  it("can switch which task a node references, or create a new one from the picker", async () => {
    const { user } = await open();
    await user.click(screen.getByRole("button", { name: "Change which task this node references" }));
    await user.type(screen.getByPlaceholderText(/Search tasks/), "brand-new-task");
    await user.click(screen.getByRole("button", { name: /Create new task "brand-new-task"/ }));
    await waitFor(() => expect(be.called("POST", "/tasks").some((c) => (c.body as { name: string }).name === "brand-new-task")).toBe(true));
    expect(await screen.findByText("brand-new-task")).toBeInTheDocument();
  });

  // ---- ADR 0073: plain "Save" writes a mutable draft; "Save as new version" stays explicit -----

  it("plain Save writes a task draft without creating a version", async () => {
    const { user } = await open();
    const max = screen.getByLabelText("Max retries");
    await user.clear(max);
    await user.type(max, "3");
    const form = screen.getByRole("form", { name: /Configure task/ });
    await user.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(be.called("PUT", "/tasks/t2/draft")).toHaveLength(1));
    expect(be.called("POST", "/tasks/t2/versions")).toHaveLength(0); // no version was created
    const sent = be.called("PUT", "/tasks/t2/draft")[0].body as { config: { retry: { maxRetries: number } } };
    expect(sent.config.retry.maxRetries).toBe(3);
    expect(await screen.findByText(/"Always follow latest" references pick this up immediately/)).toBeInTheDocument();
  });

  it("a node referencing a task by 'latest' loads that task's current draft, not just its latest version", async () => {
    const t = be.addTask("loader", { code: { type: "inline", source: "x=1" }, runner: "base", retry: null, params: {}, timeoutSeconds: 3600, platformAccess: false });
    be.addPipeline("etl", [{ key: "a", taskId: t.id, taskVersion: "latest", dependsOn: [], position: { x: 0, y: 0 } }]);
    // A plain Save on the task, after the pipeline was wired to it — never promoted to a version.
    be.saveTaskDraft(t.id, { code: { type: "inline", source: "x=1" }, runner: "base", retry: { maxRetries: 7, delaySeconds: 0, backoff: "fixed" }, params: {}, timeoutSeconds: 3600, platformAccess: false });
    mount("editor", "/pipeline/pipelines/p1");
    await userEvent.setup().click(await screen.findByTestId("node-a"));
    expect(await screen.findByLabelText("Max retries")).toHaveValue(7); // the draft's value, not v1's
  });
});

describe("code catalog picker", () => {
  async function pickCatalog() {
    const user = userEvent.setup();
    be.addPipeline("etl", etl());
    mount("editor", "/pipeline/pipelines/p1");
    await user.click(await screen.findByTestId("node-clean"));
    await user.click(await screen.findByLabelText("From the code catalog"));
    return user;
  }

  it("references a catalog entry as {entryId, version} — the narrow shape of ADR 0010", async () => {
    const user = await pickCatalog();
    await user.click(await screen.findByRole("button", { name: "Use loader" }));
    await user.click(screen.getByRole("button", { name: "Save as new task version" }));
    await waitFor(() => expect(be.called("POST", "/tasks/t2/versions")).toHaveLength(1));
    const sent = be.called("POST", "/tasks/t2/versions")[0].body as { config: { code: Record<string, unknown> } };
    expect(sent.config.code).toMatchObject({ type: "catalog", entryId: "e1", version: "latest" }); // the server pins "latest" on save
  });

  it("offers the entry's versions for pinning", async () => {
    const user = await pickCatalog();
    await user.click(await screen.findByRole("button", { name: "Use loader" }));
    const sel = await screen.findByLabelText("Version", { selector: "#catalog-version" });
    await user.selectOptions(sel, "1.0.0");
    expect(sel).toHaveValue("1.0.0");
  });

  it("a missing catalog is a warning beside the picker, never a broken builder — storage still works", async () => {
    be.catalogDown = true;
    const user = await pickCatalog();
    expect(await screen.findByText(/The code catalog could not be reached/)).toBeInTheDocument();
    expect(screen.getByText(/Pick a storage reference instead/)).toBeInTheDocument();
    // the rest of the builder is untouched
    expect(screen.getByRole("button", { name: /\+ Add task/ })).toBeEnabled();
    await user.click(screen.getByLabelText("From storage"));
    expect(await screen.findByRole("button", { name: /Use tasks\/a\.py/ })).toBeInTheDocument();
  });

  it("routes a catalog-field validation error to the picker, not just the generic banner", async () => {
    const user = await pickCatalog();
    be.failNext.set("POST /tasks/t2/versions", { status: 422, error: "must not be empty", field: "code.catalog.entryId" });
    await user.click(screen.getByRole("button", { name: "Save as new task version" }));
    const codeSection = await screen.findByRole("region", { name: "Code" });
    expect(within(codeSection).getByText("must not be empty")).toBeInTheDocument();
  });
});

describe("storage code picker (ADR 0063)", () => {
  async function pickStorage() {
    const user = userEvent.setup();
    be.addPipeline("etl", etl());
    mount("editor", "/pipeline/pipelines/p1");
    await user.click(await screen.findByTestId("node-clean"));
    await user.click(await screen.findByLabelText("From storage"));
    return user;
  }

  it("references a storage object as {backendId, path} — the narrow shape of ADR 0063", async () => {
    const user = await pickStorage();
    await user.click(await screen.findByRole("button", { name: "Use tasks/a.py" }));
    await user.click(screen.getByRole("button", { name: "Save as new task version" }));
    await waitFor(() => expect(be.called("POST", "/tasks/t2/versions")).toHaveLength(1));
    const sent = be.called("POST", "/tasks/t2/versions")[0].body as { config: { code: Record<string, unknown> } };
    expect(sent.config.code).toEqual({ type: "storage", backendId: "b1", path: "tasks/a.py" });
  });

  it("filters objects by a typed path prefix", async () => {
    const user = await pickStorage();
    await screen.findByRole("button", { name: "Use tasks/a.py" });
    await user.type(screen.getByLabelText("Filter by path prefix"), "tasks/b");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Use tasks/a.py" })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Use tasks/b.sql" })).toBeInTheDocument();
  });

  it("a missing storage backend list is a warning beside the picker, never a broken builder", async () => {
    be.storageDown = true;
    await pickStorage();
    expect(await screen.findByText(/Storage backends could not be listed/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /\+ Add task/ })).toBeEnabled();
  });
});

describe("pipeline scheduling (ADR 0071 — owned directly by Pipeline, no more Job)", () => {
  async function openSchedule() {
    const user = userEvent.setup();
    mount("editor", "/pipeline/pipelines/p1");
    await user.click(await screen.findByRole("button", { name: "Schedule" }));
    return user;
  }

  it("saves a cron schedule", async () => {
    be.addPipeline("etl", etl());
    const user = await openSchedule();
    await user.click(screen.getByLabelText("Run on a schedule"));
    await user.selectOptions(screen.getByLabelText("Frequency"), "Custom (cron expression)");
    const cron = screen.getByLabelText(/Cron expression/);
    await user.clear(cron);
    await user.type(cron, "0 2 * * *");
    const tz = screen.getByLabelText("Timezone");
    await user.clear(tz);
    await user.type(tz, "America/Toronto");
    await user.click(screen.getByRole("button", { name: "Save schedule" }));
    await waitFor(() => expect(be.called("PUT", "/pipelines/p1/schedule")).toHaveLength(1));
    expect(be.called("PUT", "/pipelines/p1/schedule")[0].body).toEqual({
      schedule: { type: "cron", cron: "0 2 * * *", timezone: "America/Toronto", enabled: true },
      allowConcurrentRuns: false,
      roleCeiling: "editor", // the default: read and write, never owner
      pinnedVersion: null,
    });
  });

  it("saves a sub-minute interval trigger (ADR 0065)", async () => {
    be.addPipeline("etl", etl());
    const user = await openSchedule();
    await user.click(screen.getByLabelText("Run on a schedule"));
    await user.selectOptions(screen.getByLabelText("Frequency"), "Every N seconds");
    expect(screen.queryByLabelText("Timezone")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save schedule" }));
    await waitFor(() => expect(be.called("PUT", "/pipelines/p1/schedule")).toHaveLength(1));
    expect((be.called("PUT", "/pipelines/p1/schedule")[0].body as { schedule: unknown }).schedule).toEqual({ type: "interval", seconds: 5, enabled: true });
  });

  it("can pin the pipeline to a specific version", async () => {
    be.addPipeline("etl", etl());
    be.saveVersion("p1", [be.ref("a")]);
    const user = await openSchedule();
    await user.selectOptions(await screen.findByLabelText("Version"), "1");
    await user.click(screen.getByRole("button", { name: "Save schedule" }));
    await waitFor(() => expect(be.called("PUT", "/pipelines/p1/schedule")).toHaveLength(1));
    expect((be.called("PUT", "/pipelines/p1/schedule")[0].body as { pinnedVersion: number }).pinnedVersion).toBe(1);
  });

  it("shows when each version was saved in the schedule tab's version-pin dropdown", async () => {
    const p = be.addPipeline("etl", etl());
    const version = be.versions.get(p.id)![0];
    await openSchedule();
    const option = (await screen.findByRole("option", { name: /^Pin to v1/ })) as HTMLOptionElement;
    expect(option.text).toContain(formatTime(version.createdAt));
  });

  it("maps a bad-schedule error onto the cron field", async () => {
    be.addPipeline("etl", etl());
    be.failNext.set("PUT /pipelines/p1/schedule", { status: 422, error: "cron must have exactly 5 fields: minute hour day-of-month month day-of-week", field: "schedule.cron" });
    const user = await openSchedule();
    await user.click(screen.getByLabelText("Run on a schedule"));
    await user.click(screen.getByRole("button", { name: "Save schedule" }));
    expect(await screen.findByText(/cron must have exactly 5 fields/)).toBeInTheDocument();
  });
});
