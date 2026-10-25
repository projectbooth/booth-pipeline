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

/** Projects in display order: each top-level project followed by its children, active ones first and
 *  archived ones after (the contract's order). `depth` is 0 for a parent, 1 for a child. */
export function orderedProjects(projects: Project[]): { project: Project; depth: number }[] {
  const tops = projects.filter((p) => !p.parent || !projects.some((q) => q.slug === p.parent));
  const order = (list: Project[]) => [...list.filter((p) => !p.archived).sort(byName), ...list.filter((p) => p.archived).sort(byName)];
  return order(tops).flatMap((top) => [
    { project: top, depth: 0 },
    ...order(projects.filter((p) => p.parent === top.slug)).map((project) => ({ project, depth: 1 })),
  ]);
}

export function projectName(projects: Project[], slug: string): string {
  return projects.find((p) => p.slug === slug)?.name ?? slug;
}

export interface ProjectGroup<T> {
  /** null: "No project". */
  slug: string | null;
  label: string;
  depth: number;
  archived: boolean;
  /** Assigned to a slug the registry doesn't list (removed outside Booth's lifecycle, or another workspace's). */
  unknown: boolean;
  items: T[];
}

/** The contract's display rules: a heading per project in display order, a parent's own items under
 *  its heading above its children's headings, archived projects after active ones (and marked), slugs
 *  the registry doesn't know after those, and "No project" last. A project heading appears only when it
 *  or one of its children has items. */
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
    const childHasItems = depth === 0 && projects.some((c) => c.parent === project.slug && (bySlug.get(c.slug)?.length ?? 0) > 0);
    if (own.length === 0 && !childHasItems) continue;
    groups.push({ slug: project.slug, label: project.name, depth, archived: project.archived, unknown: false, items: own });
  }
  // Archived top-level projects (and their children) already sit after the active ones in `ordered`.
  const known = new Set(projects.map((p) => p.slug));
  for (const slug of [...bySlug.keys()].filter((s) => !known.has(s)).sort()) {
    groups.push({ slug, label: slug, depth: 0, archived: false, unknown: true, items: bySlug.get(slug)! });
  }
  if (none.length) groups.push({ slug: null, label: "No project", depth: 0, archived: false, unknown: false, items: none });
  return groups;
}

/** "Suggest projects from names": the project whose slug is the longest prefix of `name` on a "-"
 *  boundary (`finance-dev-silver` → `finance-dev` over `finance`), among active projects. Never applied
 *  by itself: the person confirms each one. */
export function suggestProject(name: string, projects: Project[]): Project | null {
  const lower = name.toLowerCase();
  let best: Project | null = null;
  for (const p of projects) {
    if (p.archived) continue;
    if (lower === p.slug || lower.startsWith(`${p.slug}-`)) {
      if (!best || p.slug.length > best.slug.length) best = p;
    }
  }
  return best;
}
