import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { keepProject } from "../navigation";
import { groupByProject, orderedProjects, suggestProject } from "../projects";
import type { Project } from "../types";
import { renderApp } from "./render";
import { FakeBackend } from "./testUtils";

// ADR 0124 (contracts/projects.md): projects on pipelines and tasks, grouping and filtering by them, and
// none of it showing when booth-core has no registry.

const P = (slug: string, name: string, parent: string | null = null, archived = false): Project => ({ slug, name, parent, archived });
const REGISTRY: Project[] = [
  P("nfl", "NFL"),
  P("finance", "Finance"),
  P("finance-prod", "Finance prod", "finance"),
  P("finance-dev", "Finance dev", "finance"),
  P("old", "Old stuff", null, true),
];

let b: FakeBackend;
beforeEach(() => {
  b = new FakeBackend();
  localStorage.clear();
  window.history.replaceState({}, "", "/");
});
afterEach(() => vi.unstubAllGlobals());

function seed() {
  const add = (name: string, project: string | null) => {
    const p = b.addPipeline(name, [b.ref(`${name}-t`)]);
    p.project = project;
    return p;
  };
  return {
    shared: add("finance-shared", "finance"),
    dev: add("finance-dev-silver", "finance-dev"),
    prod: add("finance-prod", "finance-prod"),
    nfl: add("nfl-catchup", "nfl"),
    old: add("legacy", "old"),
    ghost: add("ghost-thing", "ghost"),
    loose: add("loose", null),
  };
}

describe("the rules", () => {
  it("orders parents before their children, archived after active", () => {
    expect(orderedProjects(REGISTRY).map(({ project, depth }) => `${depth}:${project.slug}`)).toEqual([
      "0:finance",
      "1:finance-dev",
      "1:finance-prod",
      "0:nfl",
      "0:old",
    ]);
  });

  it("groups by the contract's display rules", () => {
    const items = [
      { n: "a", p: "finance-dev" },
      { n: "b", p: "finance" },
      { n: "c", p: null },
      { n: "d", p: "old" },
      { n: "e", p: "ghost" },
      { n: "f", p: "finance-prod" },
    ];
    const groups = groupByProject(items, (i) => i.p, REGISTRY);
    expect(groups.map((g) => [g.label, g.depth, g.archived, g.unknown, g.items.map((i) => i.n).join()])).toEqual([
      ["Finance", 0, false, false, "b"],
      ["Finance dev", 1, false, false, "a"],
      ["Finance prod", 1, false, false, "f"],
      ["Old stuff", 0, true, false, "d"],
      ["Not in the registry", 0, false, true, "e"],
      ["No project", 0, false, false, "c"],
    ]);
    // A parent with nothing of its own still heads its children's items; empty projects don't appear.
    expect(groupByProject([{ n: "a", p: "finance-dev" }], (i) => i.p, REGISTRY).map((g) => [g.label, g.items.length])).toEqual([
      ["Finance", 0],
      ["Finance dev", 1],
    ]);
  });

  it("puts an archived parent with an active child among the active projects, open and still marked", () => {
    const reg = [P("finance", "Finance", null, true), P("finance-dev", "Finance dev", "finance"), P("nfl", "NFL"), P("zz", "Zz", null, true)];
    expect(orderedProjects(reg).map(({ project }) => project.slug)).toEqual(["finance", "finance-dev", "nfl", "zz"]);
    const groups = groupByProject([{ p: "finance-dev" }, { p: "zz" }], (i) => i.p, reg);
    expect(groups.map((g) => [g.label, g.archived, g.collapsed])).toEqual([
      ["Finance", true, false],
      ["Finance dev", false, false],
      ["Zz", true, true],
    ]);
  });

  it("treats null, an absent field and \"\" all as no parent", () => {
    const reg = [P("a", "A"), { ...P("b", "B"), parent: "" }, { slug: "c", name: "C", archived: false } as unknown as Project, P("d", "D", "a")];
    expect(orderedProjects(reg).map(({ project, depth }) => `${depth}:${project.slug}`)).toEqual(["0:a", "1:d", "0:b", "0:c"]);
  });

  it("shows no headings at all when nothing is assigned", () => {
    expect(groupByProject([{ p: null }, { p: null }], (i) => i.p, REGISTRY)).toEqual([]);
  });

  it("suggests the longest active project slug on a '-' boundary, never an archived one", () => {
    expect(suggestProject("finance-dev-silver", REGISTRY)?.slug).toBe("finance-dev");
    expect(suggestProject("finance-reports", REGISTRY)?.slug).toBe("finance");
    expect(suggestProject("nfl", REGISTRY)?.slug).toBe("nfl");
    expect(suggestProject("nflx-catchup", REGISTRY)).toBeNull();
    expect(suggestProject("old-thing", REGISTRY)).toBeNull();
    // The SLUG, exactly: never the display name, never a different case.
    expect(suggestProject("Finance-reports", REGISTRY)).toBeNull();
    expect(suggestProject("NFL-catchup", REGISTRY)).toBeNull();
    expect(suggestProject("finance dev-x", REGISTRY)).toBeNull();
  });

  it("keeps the shell's ?project= on the module's own navigation", () => {
    expect(keepProject("/pipeline/pipelines/p1", "?project=finance")).toBe("/pipeline/pipelines/p1?project=finance");
    expect(keepProject("/pipeline/runs/r1?task=a", "?project=finance")).toBe("/pipeline/runs/r1?task=a&project=finance");
    expect(keepProject("/pipeline/pipelines", "")).toBe("/pipeline/pipelines");
  });
});

describe("with no registry in core", () => {
  it("shows nothing about projects and groups by name", async () => {
    seed();
    renderApp(b, { path: "/pipeline/pipelines" });
    expect(await screen.findByRole("button", { name: "finance" })).toBeInTheDocument(); // the name grouping
    const groupBy = screen.getByLabelText("Group by");
    expect(within(groupBy).queryByRole("option", { name: "Project" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Suggest projects/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "+ New pipeline" })[0]);
    expect(screen.queryByLabelText("Project")).not.toBeInTheDocument();
  });

  it("leaves a pipeline's project alone when its details are saved", async () => {
    const { dev } = seed();
    renderApp(b, { path: `/pipeline/pipelines/${dev.id}` });
    await userEvent.click(await screen.findByRole("button", { name: "More pipeline actions" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Edit details…" }));
    expect(screen.queryByLabelText("Project")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save details" }));
    await waitFor(() => expect(b.called("PUT", `/pipelines/${dev.id}`)).toHaveLength(1));
    expect("project" in (b.called("PUT", `/pipelines/${dev.id}`)[0].body as object)).toBe(false);
  });
});

describe("with the registry", () => {
  beforeEach(() => {
    b.projects = REGISTRY;
  });

  it("groups the list by project, by default, as the contract shows it", async () => {
    seed();
    renderApp(b, { path: "/pipeline/pipelines" });
    const finance = await screen.findByRole("button", { name: "Finance" });
    expect(screen.getByLabelText("Group by")).toHaveValue("project");
    expect(screen.getByRole("button", { name: "Old stuff" })).toHaveAttribute("aria-expanded", "false");
    // Children are indented under their parent; the archived project is marked and collapsed.
    expect(screen.getByRole("button", { name: "Finance dev" }).closest("td")!.className).toContain("pl-9");
    expect(finance.closest("td")!.className).toContain("pl-3");
    const old = screen.getByRole("button", { name: "Old stuff" });
    expect(old.closest("td")).toHaveTextContent("archived");
    expect(screen.queryByRole("link", { name: "legacy" })).not.toBeInTheDocument();
    await userEvent.click(old);
    expect(screen.getByRole("link", { name: "legacy" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Not in the registry: ghost" })).toHaveAttribute("aria-expanded", "true");
    // Order down the table: parent, its children, NFL, archived, unknown, No project last.
    const order = [...document.querySelectorAll("td[colspan] button[aria-expanded]")].map((h) => h.textContent?.replace(/^[▾▸]/, ""));
    expect(order).toEqual(["Finance", "Finance dev", "Finance prod", "NFL", "Old stuff", "Not in the registry: ghost", "No project"]);
  });

  it("filters by the shell's ?project=, including the children, and keeps it on navigation", async () => {
    const { dev } = seed();
    window.history.replaceState({}, "", "/pipeline/pipelines?project=finance");
    renderApp(b, { path: "/pipeline/pipelines?project=finance" });
    expect(await screen.findByText(/Showing/)).toHaveTextContent("Showing Finance and its sub-projects");
    await screen.findByRole("link", { name: "finance-dev-silver" });
    expect(b.called("GET", "/pipelines?").some((c) => c.path.includes("project=finance"))).toBe(true);
    expect(screen.queryByRole("link", { name: "nfl-catchup" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "loose" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "finance-dev-silver" }));
    expect(window.location.pathname).toBe(`/pipeline/pipelines/${dev.id}`);
    expect(window.location.search).toBe("?project=finance");
  });

  it("shows a selected sub-project under its parent's heading, with only its own items", async () => {
    seed();
    window.history.replaceState({}, "", "/pipeline/pipelines?project=finance-dev");
    renderApp(b, { path: "/pipeline/pipelines?project=finance-dev" });
    await screen.findByRole("link", { name: "finance-dev-silver" });
    const order = [...document.querySelectorAll("td[colspan] button[aria-expanded]")].map((h) => h.textContent?.replace(/^[▾▸]/, ""));
    expect(order).toEqual(["Finance", "Finance dev"]);
    expect(screen.queryByRole("link", { name: "finance-shared" })).not.toBeInTheDocument();
  });

  it("shows no project headings when the registry answers but nothing is assigned", async () => {
    b.addPipeline("one", [b.ref("x")]);
    b.addPipeline("two", [b.ref("y")]);
    renderApp(b, { path: "/pipeline/pipelines" });
    await screen.findByRole("link", { name: "one" });
    expect(screen.getByLabelText("Group by")).toHaveValue("project");
    expect(document.querySelectorAll("td[colspan] button[aria-expanded]")).toHaveLength(0);
  });

  it.each([["nope"], ["Not A Slug"]])("says there is no project called %s, with an empty list", async (slug) => {
    seed();
    b.addTask("finance-dev-loader").project = "finance-dev";
    const qs = `?project=${encodeURIComponent(slug)}`;
    window.history.replaceState({}, "", `/pipeline/pipelines${qs}`);
    const view = renderApp(b, { path: `/pipeline/pipelines${qs}` });
    expect(await screen.findByText(`There is no project called "${slug}".`)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText("No pipelines yet")).not.toBeInTheDocument();
    view.unmount();
    window.history.replaceState({}, "", `/pipeline/tasks${qs}`);
    renderApp(b, { path: `/pipeline/tasks${qs}` });
    expect(await screen.findByText(`There is no project called "${slug}".`)).toBeInTheDocument();
    expect(screen.queryByText("finance-dev-loader")).not.toBeInTheDocument();
  });

  it("offers active projects, children indented, on a new pipeline", async () => {
    renderApp(b, { path: "/pipeline/pipelines" });
    await userEvent.click((await screen.findAllByRole("button", { name: "+ New pipeline" }))[0]);
    const select = screen.getByLabelText("Project");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "No project",
      "Finance",
      "    Finance dev",
      "    Finance prod",
      "NFL",
    ]);
    await userEvent.type(screen.getByLabelText(/^Name/), "finance-dev-gold");
    await userEvent.selectOptions(select, "finance-dev");
    await userEvent.click(screen.getByRole("button", { name: "Create and open" }));
    await waitFor(() => expect(b.called("POST", "/pipelines")).toHaveLength(1));
    expect(b.called("POST", "/pipelines")[0].body).toMatchObject({ name: "finance-dev-gold", project: "finance-dev" });
  });

  it("keeps an archived project on an item that has it, and can change it", async () => {
    const { old } = seed();
    renderApp(b, { path: `/pipeline/pipelines/${old.id}` });
    expect(await screen.findByText("Old stuff (archived)")).toBeInTheDocument(); // the header badge
    await userEvent.click(screen.getByRole("button", { name: "More pipeline actions" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Edit details…" }));
    const select = screen.getByLabelText("Project");
    expect(select).toHaveValue("old");
    expect(within(select).getByRole("option", { name: "Old stuff (archived)" })).toBeInTheDocument();
    await userEvent.selectOptions(select, "nfl");
    await userEvent.click(screen.getByRole("button", { name: "Save details" }));
    await waitFor(() => expect(b.called("PUT", `/pipelines/${old.id}`)).toHaveLength(1));
    expect(b.called("PUT", `/pipelines/${old.id}`)[0].body).toMatchObject({ project: "nfl" });
  });

  it("sets a task's project, and filters the task list by ?project=", async () => {
    const t1 = b.addTask("finance-dev-loader");
    t1.project = "finance-dev";
    b.addTask("nfl-loader").project = "nfl";
    renderApp(b, { path: "/pipeline/tasks?project=finance" });
    await screen.findByText("finance-dev-loader");
    expect(screen.queryByText("nfl-loader")).not.toBeInTheDocument();
    expect(b.called("GET", "/tasks?").some((c) => c.path.includes("project=finance"))).toBe(true);
  });

  it("suggests projects from names, and assigns only what is confirmed", async () => {
    seed();
    const lone = b.addPipeline("nfl-weekly", [b.ref("w")]);
    b.addTask("finance-dev-cleaner");
    renderApp(b, { path: "/pipeline/pipelines" });
    await userEvent.click(await screen.findByRole("button", { name: "Suggest projects from names…" }));
    const panel = await screen.findByRole("region", { name: "Suggest projects from names" });
    const table = await within(panel).findByRole("table", { name: "Suggested projects" });
    const text = table.textContent ?? "";
    expect(text).toContain("nfl-weekly");
    expect(text).toContain("finance-dev-cleaner");
    const suggestedNames = within(table).getAllByRole("row").map((r) => (r as HTMLTableRowElement).cells[1]?.textContent);
    expect(suggestedNames).not.toContain("finance-prod"); // already in a project: never moved
    expect(b.called("PUT", "/pipelines/")).toHaveLength(0); // nothing assigned by itself
    expect(within(table).getByLabelText("Assign finance-dev-cleaner to Finance dev")).not.toBeChecked();
    await userEvent.click(within(table).getByRole("button", { name: "Assign nfl-weekly now" }));
    await waitFor(() => expect(b.called("PUT", `/pipelines/${lone.id}`)).toHaveLength(1));
    expect(b.called("PUT", `/pipelines/${lone.id}`)[0].body).toMatchObject({ project: "nfl", name: "nfl-weekly" });
    await userEvent.click(within(panel).getByRole("button", { name: /Tick all/ }));
    await userEvent.click(within(panel).getByRole("button", { name: /Assign the \d+ ticked/ }));
    await waitFor(() => expect(within(panel).queryByRole("table")).not.toBeInTheDocument());
    expect(b.called("PUT", "/tasks/").some((c) => (c.body as { project?: string }).project === "finance-dev")).toBe(true);
  });
});
