import { useCallback, useEffect, useRef, useState } from "react";
import { PipelineApp } from "../PipelineApp";
import type { WorkspaceRole } from "../types";

const ROLES: WorkspaceRole[] = ["owner", "editor", "viewer"];

// Stand-in for booth-design's real shell — local-dev scaffolding only, never shipped (the same idea
// as booth-catalog's and booth-module-store's dev harnesses). It exists so this package can be
// built and looked at standalone against a locally running backend, and doubles as a manual check
// of the props contract (ADR 0031/0033): workspace/role/theme/getAccessToken.
//
// The backend trusts the X-Booth-* headers only alongside a verifiable bearer token, so to use
// this against a real backend paste a token from your OIDC provider (hack/dev-keycloak.sh token
// bob). The harness also mimics the shell's routing just enough: PipelineApp reads window.location
// exactly as it would under booth-design's router, and this starts the address bar under the
// module's navPath (/pipeline), which the real shell does by routing there.
export function DevShell() {
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    try {
      return (localStorage.getItem("pipeline-dev-theme") as "light" | "dark") ?? "light";
    } catch {
      return "light";
    }
  });
  const [workspace, setWorkspace] = useState("acme");
  useState(() => {
    if (!window.location.pathname.startsWith("/pipeline")) window.history.replaceState({}, "", "/pipeline/pipelines");
  });
  const [role, setRole] = useState<WorkspaceRole>("owner");
  const [tokenInput, setTokenInput] = useState("");

  // A ref, not state read directly, so getAccessToken keeps a stable identity across renders while
  // always returning whatever was last typed — the same shape as a real in-memory token store
  // (ADR 0033).
  const tokenRef = useRef(tokenInput);
  useEffect(() => {
    tokenRef.current = tokenInput;
  }, [tokenInput]);
  const getAccessToken = useCallback(() => tokenRef.current || null, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("pipeline-dev-theme", theme);
    } catch {
      // best-effort only
    }
  }, [theme]);

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-950">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-white px-6 py-3 dark:border-slate-800 dark:bg-slate-900">
        <div>
          <p className="text-xs uppercase tracking-wide text-slate-400">Dev harness — not the real shell</p>
          <h1 className="text-lg font-semibold text-slate-900 dark:text-slate-100">Pipelines</h1>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-xs text-slate-500 dark:text-slate-400">
            Workspace
            <input
              type="text"
              value={workspace}
              onChange={(e) => setWorkspace(e.target.value)}
              className="ml-1.5 w-28 rounded-md border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
            />
          </label>
          <label className="text-xs text-slate-500 dark:text-slate-400">
            Role (UI gating only)
            <select
              value={role}
              onChange={(e) => setRole(e.target.value as WorkspaceRole)}
              className="ml-1.5 rounded-md border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-500 dark:text-slate-400">
            Access token
            <input
              type="password"
              value={tokenInput}
              onChange={(e) => setTokenInput(e.target.value)}
              placeholder="paste a bearer token"
              className="ml-1.5 w-56 rounded-md border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
            />
          </label>
          <button
            type="button"
            onClick={() => setTheme(theme === "light" ? "dark" : "light")}
            className="rounded-md border border-slate-300 px-2 py-1 text-sm text-slate-700 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
          >
            {theme === "light" ? "Dark" : "Light"} mode
          </button>
        </div>
      </header>
      <div className="mx-auto max-w-7xl px-6 py-6">
        {/* keyed on workspace + token so pasting a token (or switching workspace) re-fetches everything */}
        <PipelineApp key={`${workspace}:${tokenInput}`} workspace={workspace} role={role} theme={theme} getAccessToken={getAccessToken} />
      </div>
    </div>
  );
}
