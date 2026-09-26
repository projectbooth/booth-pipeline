import { useMemo } from "react";
import type { ApiContext, GetAccessToken } from "./api/client";
import type { ViewCtx } from "./context";
import { DEFAULT_BASE_PATH, defaultNavigate, parseRoute, routePath, sectionOf, useLocation, type Route, type Section } from "./navigation";
import type { WorkspaceRole } from "./types";
import { ErrorBoundary } from "./ui/ErrorBoundary";
import { Link } from "./ui/primitives";
import { PipelineDetail } from "./views/pipeline/PipelineDetail";
import { PipelineList } from "./views/PipelineList";
import { RunDetail } from "./views/RunDetail";
import { TaskDetail } from "./views/task/TaskDetail";
import { TaskList } from "./views/TaskList";

/**
 * Props contract agreed with booth-design and pinned into contracts/ui-integration.md by ADR 0031
 * (workspace/role/theme) and ADR 0033 (getAccessToken): plain React props, not shared context, so
 * this package never depends on anything booth-design exports (that would invert the dependency
 * direction ADR 0030 established). Unchanged by the ADR 0074 rebuild — booth-design needs nothing
 * beyond a version-pin bump.
 *
 * The optional props below the contract four are this package's own, none required.
 */
export interface PipelineAppProps {
  /** Active workspace slug (ADR 0025) — required for every API call this makes. */
  workspace: string;
  /** Caller's role (ADR 0025). Gates which controls this UI offers; booth-pipeline's backend
   *  independently enforces the same rules on every request (ADR 0041), so this is a UX nicety
   *  and never the security boundary. */
  role: WorkspaceRole;
  theme: "dark" | "light";
  /** Returns booth-design's current bearer token (ADR 0032), or null when not authenticated.
   *  Called fresh before every request, never cached (ADR 0033). */
  getAccessToken: GetAccessToken;

  /** The manifest's navPath: every pipeline page lives beneath it. Default "/pipeline". */
  basePath?: string;
  /** How to move between pages. Defaults to in-place history navigation, which the shell's router
   *  picks up without a page reload; pass the shell's own navigate function if it offers one. */
  onNavigate?: (path: string) => void;
}

const SECTIONS: { section: Section; label: string; route: Route }[] = [
  { section: "pipelines", label: "Pipelines", route: { name: "pipelines" } },
  { section: "tasks", label: "Tasks", route: { name: "tasks" } },
];

/**
 * The native-mode component booth-design's shell mounts for booth-pipeline (ADR 0030), published
 * as @projectbooth/pipeline-ui. Screens are specified in docs/decisions/0016.
 *
 * No outer padding on the root (ADR 0072): the shell owns the gutter around every native module.
 */
export function PipelineApp({ workspace, role, theme, getAccessToken, basePath = DEFAULT_BASE_PATH, onNavigate = defaultNavigate }: PipelineAppProps) {
  const { pathname, search } = useLocation();
  const route = parseRoute(pathname, search, basePath);

  // Stable across renders unless the workspace or token accessor actually changes, so effects keyed
  // on it don't refire spuriously.
  const api = useMemo<ApiContext>(() => ({ workspace, getAccessToken }), [workspace, getAccessToken]);
  const v = useMemo<ViewCtx>(
    () => ({
      api,
      canWrite: role === "owner" || role === "editor",
      theme,
      href: (r) => routePath(r, basePath),
      go: (r) => onNavigate(routePath(r, basePath)),
      goPath: onNavigate,
    }),
    [api, role, theme, basePath, onNavigate],
  );

  const active = sectionOf(route);

  return (
    <div data-theme={theme} className="flex flex-col gap-6 text-slate-900 dark:text-slate-100">
      <nav aria-label="Pipeline sections" className="flex gap-1 border-b border-slate-200 dark:border-slate-800">
        {SECTIONS.map((s) => (
          <Link
            key={s.section}
            href={v.href(s.route)}
            onNavigate={v.goPath}
            aria-current={active === s.section ? "page" : undefined}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium ${
              active === s.section
                ? "border-indigo-600 text-slate-900 dark:border-indigo-400 dark:text-slate-50"
                : "border-transparent text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200"
            }`}
          >
            {s.label}
          </Link>
        ))}
      </nav>
      <main className="min-w-0">
        {/* Keyed on the entity, not the full path: switching tabs inside one pipeline must NOT
            remount it, or unsaved canvas edits would be lost on every tab switch (0016 §4 S2a). */}
        <ErrorBoundary key={boundaryKey(route)}>{renderRoute(route, v)}</ErrorBoundary>
      </main>
    </div>
  );
}

function boundaryKey(route: Route): string {
  switch (route.name) {
    case "pipeline":
    case "task":
    case "run":
      return `${route.name}:${route.id}`;
    default:
      return route.name;
  }
}

function renderRoute(route: Route, v: ViewCtx) {
  switch (route.name) {
    case "pipelines":
      return <PipelineList v={v} />;
    case "pipeline":
      return <PipelineDetail key={route.id} v={v} pipelineId={route.id} tab={route.tab ?? "dag"} version={route.version} />;
    case "tasks":
      return <TaskList v={v} />;
    case "task":
      return <TaskDetail key={route.id} v={v} taskId={route.id} tab={route.tab ?? "code"} version={route.version} />;
    case "run":
      return <RunDetail key={route.id} v={v} runId={route.id} initialTask={route.task} />;
  }
}
