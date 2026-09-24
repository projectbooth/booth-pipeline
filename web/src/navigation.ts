import { useSyncExternalStore } from "react";

// The shell (booth-design) matches a module's navPath — and anything beneath it — to the one
// component registered for the module id, but NativeModuleProps (ADR 0031/0033) carries no route.
// So this package reads window.location itself: the shell is a browser-router SPA, so the address
// bar is the one source of truth that is always right, and it needs no contract change. This is
// the same workaround booth-catalog and booth-storage use; if the shell ever passes a route prop,
// this file is the only place that changes.

export const DEFAULT_BASE_PATH = "/pipeline";

export type Route =
  | { name: "pipelines" }
  | { name: "pipeline"; id: string; version?: number }
  | { name: "tasks" }
  | { name: "task"; id: string }
  | { name: "run"; id: string };

export type Section = "pipelines" | "tasks";

export function sectionOf(route: Route): Section {
  return route.name === "task" || route.name === "tasks" ? "tasks" : "pipelines";
}

/** Parses the address bar into a Route. Anything unrecognised is the pipeline list: a stale
 *  bookmark should land somewhere useful, not on an error. */
export function parseRoute(pathname: string, _search: string, basePath: string = DEFAULT_BASE_PATH): Route {
  const rel = pathname === basePath ? "" : pathname.startsWith(basePath + "/") ? pathname.slice(basePath.length + 1) : "";
  const parts = rel.split("/").filter(Boolean).map(decodeURIComponent);
  const [area, second, third, fourth] = parts;

  switch (area) {
    case "pipelines":
      if (second && third === "v" && fourth && /^\d+$/.test(fourth) && parts.length === 4) return { name: "pipeline", id: second, version: Number(fourth) };
      if (second && parts.length === 2) return { name: "pipeline", id: second };
      break;
    case "tasks":
      if (second && parts.length === 2) return { name: "task", id: second };
      if (parts.length === 1) return { name: "tasks" };
      break;
    case "runs":
      if (second && parts.length === 2) return { name: "run", id: second };
      break;
  }
  return { name: "pipelines" };
}

const e = encodeURIComponent;

/** The inverse of parseRoute. */
export function routePath(route: Route, basePath: string = DEFAULT_BASE_PATH): string {
  switch (route.name) {
    case "pipelines":
      return `${basePath}/pipelines`;
    case "pipeline":
      return route.version ? `${basePath}/pipelines/${e(route.id)}/v/${route.version}` : `${basePath}/pipelines/${e(route.id)}`;
    case "tasks":
      return `${basePath}/tasks`;
    case "task":
      return `${basePath}/tasks/${e(route.id)}`;
    case "run":
      return `${basePath}/runs/${e(route.id)}`;
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
