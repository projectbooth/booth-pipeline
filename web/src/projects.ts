import { useEffect, useState } from "react";
import { api, type ApiContext } from "./api/client";
import { useLocation } from "./navigation";
import type { Project } from "./types";

// ADR 0124 (booth-architecture contracts/projects.md): projects are booth-core's navigation labels. This
// module shows and sets them on pipelines and tasks, and groups and filters its lists by them. When the
// registry can't be read (an older core, 503, unreachable) none of it shows and the page is unchanged.

export type ProjectsState = { status: "loading" } | { status: "unavailable" } | { status: "ready"; projects: Project[] };

/** The workspace's projects, read as the user from core. Never throws: a failure is "unavailable". */
export function useProjects(ctx: ApiContext): ProjectsState {
  const [state, setState] = useState<ProjectsState>({ status: "loading" });
  useEffect(() => {
    let alive = true;
    api.projects(ctx).then(
      (projects) => alive && setState({ status: "ready", projects }),
      () => alive && setState({ status: "unavailable" }),
    );
    return () => {
      alive = false;
    };
  }, [ctx]);
  return state;
}

/** The shell's project picker selection, `?project=<slug>` on the page URL, or null. */
export function useSelectedProject(): string | null {
  const { search } = useLocation();
  return new URLSearchParams(search).get("project");
}

const byName = (a: Project, b: Project) => a.name.localeCompare(b.name) || a.slug.localeCompare(b.slug);

/** The contract: `null`, an absent field and `""` all mean "no parent". */
export const parentOf = (p: Project): string | null => p.parent || null;

/** Whether a project sorts and opens with the active ones: it isn't archived, or it is an archived parent
 *  with an active child (archiving doesn't cascade, and the child must never be hidden). */
export function shownAsActive(p: Project, projects: Project[]): boolean {
  return !p.archived || projects.some((c) => parentOf(c) === p.slug && !c.archived);
}

/** Projects in display order: each top-level project followed by its children, active ones first and
 *  archived ones after (the contract's order). `depth` is 0 for a parent, 1 for a child. */
export function orderedProjects(projects: Project[]): { project: Project; depth: number }[] {
  const tops = projects.filter((p) => !parentOf(p) || !projects.some((q) => q.slug === parentOf(p)));
  const order = (list: Project[]) => {
    const active = list.filter((p) => shownAsActive(p, projects));
    return [...active.sort(byName), ...list.filter((p) => !active.includes(p)).sort(byName)];
  };
  return order(tops).flatMap((top) => [
    { project: top, depth: 0 },
    ...order(projects.filter((p) => parentOf(p) === top.slug)).map((project) => ({ project, depth: 1 })),
  ]);
}

export function projectName(projects: Project[], slug: string): string {
  return projects.find((p) => p.slug === slug)?.name ?? slug;
}

/** The key of the one heading for slugs the registry doesn't list (not a slug: the grammar has no ":"). */
export const UNLISTED = ":unlisted";

export interface ProjectGroup<T> {
  /** null: "No project". */
  slug: string | null;
  label: string;
  depth: number;
  /** Marked "archived". */
  archived: boolean;
  /** Closed until opened: archived, except an archived parent with an active child. */
  collapsed: boolean;
  /** The "Not in the registry" heading: items whose slug core doesn't list (`unknownSlugs`). */
  unknown: boolean;
  unknownSlugs?: string[];
  items: T[];
}

/** The contract's display rules: a heading per project in display order, a parent's own items under
 *  its heading above its children's headings, archived projects after active ones (marked and collapsed;
 *  an archived parent with an active child sorts and opens with the active ones, still marked), one "Not
 *  in the registry" heading for slugs core doesn't list, and "No project" last. A project heading appears
 *  only when it or one of its children has items; with nothing assigned at all there are no headings
 *  (an empty array: the caller shows its plain list). */
export function groupByProject<T>(items: T[], projectOf: (item: T) => string | null | undefined, projects: Project[]): ProjectGroup<T>[] {
  const bySlug = new Map<string, T[]>();
  const none: T[] = [];
  for (const item of items) {
    const slug = projectOf(item);
    if (!slug) none.push(item);
    else bySlug.set(slug, [...(bySlug.get(slug) ?? []), item]);
  }
  const groups: ProjectGroup<T>[] = [];
  const ordered = orderedProjects(projects);
  for (const { project, depth } of ordered) {
    const own = bySlug.get(project.slug) ?? [];
    const childHasItems = depth === 0 && projects.some((c) => parentOf(c) === project.slug && (bySlug.get(c.slug)?.length ?? 0) > 0);
    if (own.length === 0 && !childHasItems) continue;
    const collapsed = !shownAsActive(project, projects);
    groups.push({ slug: project.slug, label: project.name, depth, archived: project.archived, collapsed, unknown: false, items: own });
  }
  // Archived top-level projects (and their children) already sit after the active ones in `ordered`.
  const known = new Set(projects.map((p) => p.slug));
  const unlisted = [...bySlug.keys()].filter((s) => !known.has(s)).sort();
  if (unlisted.length) {
    const items = unlisted.flatMap((s) => bySlug.get(s)!);
    groups.push({ slug: UNLISTED, label: "Not in the registry", depth: 0, archived: false, collapsed: false, unknown: true, unknownSlugs: unlisted, items });
  }
  if (groups.length === 0) return []; // nothing assigned: no headings, not a lone "No project"
  if (none.length) groups.push({ slug: null, label: "No project", depth: 0, archived: false, collapsed: false, unknown: false, items: none });
  return groups;
}

/** "Suggest projects from names" (the contract): the longest active project SLUG that the name equals, or
 *  starts with followed by "-" (`finance-dev-silver` → `finance-dev` over `finance`). Display names never
 *  match. Never applied by itself: the person confirms each one. */
export function suggestProject(name: string, projects: Project[]): Project | null {
  let best: Project | null = null;
  for (const p of projects) {
    if (p.archived) continue;
    if (name === p.slug || name.startsWith(`${p.slug}-`)) {
      if (!best || p.slug.length > best.slug.length) best = p;
    }
  }
  return best;
}

/** The contract: an unknown or malformed ``?project=`` shows "There is no project called …" (and an empty
 *  list). Only answerable with the registry; without it, null (the list just filters by the slug). */
export function noSuchProject(selected: string | null, state: ProjectsState): string | null {
  if (!selected) return null;
  if (!/^[a-z0-9-]{1,63}$/.test(selected)) return selected;
  return state.status === "ready" && !state.projects.some((p) => p.slug === selected) ? selected : null;
}

