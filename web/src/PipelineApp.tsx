import { useMemo } from "react";
import type { ApiContext, GetAccessToken } from "./api/client";
import { Link } from "./components/ui";
import type { ViewCtx } from "./context";
import { DEFAULT_BASE_PATH, defaultNavigate, parseRoute, routePath, sectionOf, useLocation, type Route, type Section } from "./navigation";
import type { WorkspaceRole } from "./types";
import { JobDetail, JobList, JobNew } from "./views/JobViews";
import { PipelineEditor } from "./views/PipelineEditor";
import { PipelineList } from "./views/PipelineList";
import { RunView } from "./views/RunView";

/**
 * Props contract agreed with booth-design and pinned into contracts/ui-integration.md by ADR 0031
 * (workspace/role/theme) and ADR 0033 (getAccessToken): plain React props, not shared context, so
 * this package never depends on anything booth-design exports (that would invert the dependency
 * direction ADR 0030 established).
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
  { section: "jobs", label: "Jobs", route: { name: "jobs" } },
];

/**
 * The native-mode component booth-design's shell mounts for booth-pipeline (ADR 0030), published
 * as @projectbooth/pipeline-ui: the graphical DAG builder, jobs and schedules, and per-run logs.
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
    <div data-theme={theme} className="flex flex-col gap-5 text-slate-900 dark:text-slate-100">
      <header className="border-b border-slate-200 pb-3 dark:border-slate-800">
        <nav aria-label="Pipeline sections" className="flex gap-1">
          {SECTIONS.map((s) => (
            <Link
              key={s.section}
              href={v.href(s.route)}
              onNavigate={v.goPath}
              aria-current={active === s.section ? "page" : undefined}
              className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                active === s.section ? "bg-indigo-600 text-white" : "text-slate-700 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
              }`}
            >
              {s.label}
            </Link>
          ))}
        </nav>
      </header>
      <main>{renderRoute(route, v)}</main>
    </div>
  );
}

function renderRoute(route: Route, v: ViewCtx) {
  switch (route.name) {
    case "pipelines":
      return <PipelineList v={v} />;
    case "pipeline":
      return <PipelineEditor v={v} pipelineId={route.id} version={route.version} />;
    case "jobs":
      return <JobList v={v} />;
    case "job-new":
      return <JobNew v={v} pipelineId={route.pipelineId} />;
    case "job":
      return <JobDetail v={v} jobId={route.id} />;
    case "run":
      return <RunView v={v} runId={route.id} />;
  }
}
