import { useCallback, useEffect, useRef, useState } from "react";
import { PipelineApp } from "../PipelineApp";
import type { WorkspaceRole } from "../types";

const ROLES: WorkspaceRole[] = ["owner", "editor", "viewer"];

// hack/dev-keycloak.sh's booth-local realm: its test users and their workspace roles in "acme".
const DEV_USERS = { alice: "owner", bob: "editor", carol: "viewer" } as const satisfies Record<string, WorkspaceRole>;
type DevUser = keyof typeof DEV_USERS;
const DEV_PASSWORD = "booth-dev-password";

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

  // "Sign in as": a real token from the local Keycloak hack/dev-keycloak.sh starts (its test users,
  // its documented dev password), via vite.config.ts's same-origin /dev-keycloak proxy, renewed
  // before it expires so a long manual click-through never hits a dead token.
  const [devUser, setDevUser] = useState<DevUser | "">(() => {
    try {
      const saved = localStorage.getItem("pipeline-dev-user");
      return saved && saved in DEV_USERS ? (saved as DevUser) : "";
    } catch {
      return "";
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem("pipeline-dev-user", devUser);
    } catch {
      // best-effort only
    }
  }, [devUser]);
  const [signInError, setSignInError] = useState<string | null>(null);
  useEffect(() => {
    if (!devUser) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const renew = async () => {
      try {
        const body = new URLSearchParams({ grant_type: "password", client_id: "booth-design", username: devUser, password: DEV_PASSWORD });
        const res = await fetch("/dev-keycloak/realms/booth-local/protocol/openid-connect/token", { method: "POST", body });
        if (!res.ok) throw new Error(`Keycloak said ${res.status}`);
        const tok = (await res.json()) as { access_token: string; expires_in: number };
        if (!alive) return;
        setSignInError(null);
        setTokenInput(tok.access_token);
        setRole(DEV_USERS[devUser]);
        timer = setTimeout(renew, Math.max(10, tok.expires_in - 30) * 1000);
      } catch (err) {
        if (alive) setSignInError(err instanceof Error ? err.message : String(err));
      }
    };
    void renew();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [devUser]);

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
            Sign in as (local Keycloak)
            <select
              aria-label="Sign in as"
              value={devUser}
              onChange={(e) => setDevUser(e.target.value as DevUser | "")}
              className="ml-1.5 rounded-md border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
            >
              <option value="">—</option>
              {(Object.keys(DEV_USERS) as DevUser[]).map((u) => (
                <option key={u} value={u}>
                  {u} ({DEV_USERS[u]})
                </option>
              ))}
            </select>
            {signInError && <span className="ml-1.5 text-red-600">{signInError}</span>}
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
        {!tokenInput && (
          <p role="status" className="mb-4 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
            Not signed in, so every request will fail with "missing bearer token". Pick a user under <strong>Sign in as</strong> (needs{" "}
            <code>hack/dev-keycloak.sh up</code>) or paste a token.
          </p>
        )}
        {/* keyed on workspace + identity so switching either re-fetches everything; a silent token
            renewal for the same signed-in user must NOT remount (that would drop unsaved edits) */}
        <PipelineApp key={`${workspace}:${devUser && tokenInput ? devUser : tokenInput}`} workspace={workspace} role={role} theme={theme} getAccessToken={getAccessToken} />
      </div>
    </div>
  );
}
