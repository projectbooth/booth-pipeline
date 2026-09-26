import { useSyncExternalStore } from "react";

// The shell (booth-design) matches a module's navPath — and anything beneath it — to the one
// component registered for the module id, but NativeModuleProps (ADR 0031/0033) carries no route.
// So this package reads window.location itself: the shell is a browser-router SPA, so the address
// bar is the one source of truth that is always right, and it needs no contract change. This is
// the same workaround booth-catalog and booth-storage use; if the shell ever passes a route prop,
// this file is the only place that changes.
//
// Detail-page tabs are part of the path (docs/decisions/0016 §2), so every screen can be linked
// to and reloaded directly. `/pipelines/:id` and `/pipelines/:id/v/:n` keep working as before.

export const DEFAULT_BASE_PATH = "/pipeline";

export type PipelineTab = "dag" | "runs" | "schedule" | "config" | "versions";
export type TaskTab = "code" | "config" | "versions";

export const PIPELINE_TABS: PipelineTab[] = ["dag", "runs", "schedule", "config", "versions"];
export const TASK_TABS: TaskTab[] = ["code", "config", "versions"];

export type Route =
  | { name: "pipelines" }
  | { name: "pipeline"; id: string; tab?: PipelineTab; version?: number }
  | { name: "tasks" }
  | { name: "task"; id: string; tab?: TaskTab; version?: number }
  | { name: "run"; id: string; task?: string };

export type Section = "pipelines" | "tasks";

export function sectionOf(route: Route): Section {
  return route.name === "task" || route.name === "tasks" ? "tasks" : "pipelines";
}

const isNum = (s: string | undefined): s is string => s !== undefined && /^\d+$/.test(s);

/** Parses the address bar into a Route. Anything unrecognised is the pipeline list: a stale
 *  bookmark should land somewhere useful, not on an error. */
export function parseRoute(pathname: string, search: string, basePath: string = DEFAULT_BASE_PATH): Route {
  const rel = pathname === basePath ? "" : pathname.startsWith(basePath + "/") ? pathname.slice(basePath.length + 1) : "";
  const parts = rel.split("/").filter(Boolean).map(decodeURIComponent);
  const [area, id, third, fourth] = parts;
  const query = new URLSearchParams(search);

  switch (area) {
    case "pipelines":
      if (!id) return { name: "pipelines" };
      if (parts.length === 2) return { name: "pipeline", id };
      if (parts.length === 4 && third === "v" && isNum(fourth)) return { name: "pipeline", id, version: Number(fourth) };
      if (parts.length === 3 && (PIPELINE_TABS as string[]).includes(third)) return third === "dag" ? { name: "pipeline", id } : { name: "pipeline", id, tab: third as PipelineTab };
      break;
    case "tasks": {
      if (!id) return { name: "tasks" };
      const v = query.get("version");
      const version = isNum(v ?? undefined) ? Number(v) : undefined;
      if (parts.length === 2) return { name: "task", id, version };
      if (parts.length === 3 && (TASK_TABS as string[]).includes(third)) return third === "code" ? { name: "task", id, version } : { name: "task", id, tab: third as TaskTab, version };
      break;
    }
    case "runs":
      if (id && parts.length === 2) return { name: "run", id, task: query.get("task") ?? undefined };
      break;
  }
  return { name: "pipelines" };
}

const e = encodeURIComponent;

/** The inverse of parseRoute. The default tab (DAG / Code) has no path segment of its own. */
export function routePath(route: Route, basePath: string = DEFAULT_BASE_PATH): string {
  switch (route.name) {
    case "pipelines":
      return `${basePath}/pipelines`;
    case "pipeline": {
      const base = `${basePath}/pipelines/${e(route.id)}`;
      if (route.version) return `${base}/v/${route.version}`;
      return route.tab && route.tab !== "dag" ? `${base}/${route.tab}` : base;
    }
    case "tasks":
      return `${basePath}/tasks`;
    case "task": {
      const base = `${basePath}/tasks/${e(route.id)}`;
      const path = route.tab && route.tab !== "code" ? `${base}/${route.tab}` : base;
      return route.version ? `${path}?version=${route.version}` : path;
    }
    case "run":
      return `${basePath}/runs/${e(route.id)}${route.task ? `?task=${e(route.task)}` : ""}`;
  }
}

const NAVIGATE_EVENT = "booth-pipeline:navigate";

function subscribe(onChange: () => void): () => void {
  window.addEventListener("popstate", onChange);
  window.addEventListener(NAVIGATE_EVENT, onChange);
  return () => {
    window.removeEventListener("popstate", onChange);
    window.removeEventListener(NAVIGATE_EVENT, onChange);
  };
}

/** The current pathname+search, re-rendering on back/forward and on this package's own navigations. */
export function useLocation(): { pathname: string; search: string } {
  const pathname = useSyncExternalStore(subscribe, () => window.location.pathname, () => "/");
  const search = useSyncExternalStore(subscribe, () => window.location.search, () => "");
  return { pathname, search };
}

/** Navigates to `path` inside the shell without a page reload.
 *
 *  A full reload would drop booth-design's in-memory access token (ADR 0032) and force a fresh
 *  login redirect. So: pushState, then a synthetic popstate — which is what the shell's browser
 *  router listens for to notice a location change. */
export function defaultNavigate(path: string): void {
  window.history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
  window.dispatchEvent(new Event(NAVIGATE_EVENT));
}
