import type { ApiContext } from "./api/client";
import type { Route } from "./navigation";

/** What every view needs from PipelineApp: how to call the API, whether this caller may write, and
 *  how to move around. Bundled so views take one prop and stay easy to render in tests. */
export interface ViewCtx {
  api: ApiContext;
  /** Whether write controls (save, create, run, cancel, delete) are offered. A UX nicety only —
   *  booth-pipeline's backend enforces the same rule on every write route (ADR 0041), so a hidden
   *  button is never the security boundary. */
  canWrite: boolean;
  theme: "dark" | "light";
  /** The href for a route, for real anchors. */
  href: (route: Route) => string;
  /** Navigate in-app to a route. */
  go: (route: Route) => void;
  goPath: (path: string) => void;
}
