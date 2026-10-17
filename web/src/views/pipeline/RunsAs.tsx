import { useEffect, useState } from "react";
import { api, ApiError, type ApiContext } from "../../api/client";
import type { ViewCtx } from "../../context";
import { errorMessage, useLoad } from "../../hooks";
import type { CoreServiceAccountState, OwnerInfo, Pipeline } from "../../types";
import { Banner, Button, inputClass } from "../../ui/primitives";

// Who a pipeline's scheduled runs act as (ADR 0119 amendment 3, ADR 0120). The backend enforces
// every rule; this only offers what the caller may do:
// - an editor may Take over a person-owned pipeline (become its owner);
// - a workspace owner picks the owner: themselves, or one of the workspace's service accounts
//   (booth-core's list, which only owners may read);
// - anyone sees "Owner disabled" when the owning account is disabled (core's per-account state).

/** The owner in words, as the UI says it everywhere. */
export function ownerLabel(o: OwnerInfo | null | undefined): string {
  if (!o) return "—";
  if (o.kind === "service") return `Service account ${o.id}`;
  const name = o.name || "the person who first scheduled it";
  return o.you ? `${name} (you)` : name;
}

export function canTakeOver(v: ViewCtx, p: Pipeline): boolean {
  if (!v.canWrite || p.owner?.you) return false;
  return p.owner?.kind === "service" ? v.role === "owner" : true;
}

export function TakeOver({ v, p, onDone }: { v: ViewCtx; p: Pipeline; onDone: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!canTakeOver(v, p)) return null;

  async function takeOver() {
    setBusy(true);
    setError(null);
    try {
      await api.setOwner(v.api, p.id, "me");
      setConfirming(false);
      onDone();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      {!confirming ? (
        <div>
          <Button size="sm" onClick={() => setConfirming(true)}>
            Take over
          </Button>
        </div>
      ) : (
        <div role="group" aria-label="Take over this pipeline" className="flex flex-col gap-2 rounded-md border border-slate-200 bg-slate-50 p-3 text-sm dark:border-slate-700 dark:bg-slate-800/50">
          <p>
            Its scheduled runs will act as you, with your access in this workspace (capped by the platform access cap), and only while you keep signing
            in to Booth.
          </p>
          <div className="flex gap-2">
            <Button size="sm" variant="primary" disabled={busy} onClick={takeOver}>
              {busy ? "Taking over…" : "Take over"}
            </Button>
            <Button size="sm" disabled={busy} onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
      {error && <Banner tone="error">{error}</Banner>}
    </div>
  );
}

/** The owning account's state in core, or null for a person owner (or while unknown). Cached briefly
 *  per workspace and account, so a list of pipelines asks once per account. */
const accountCache = new Map<string, { at: number; value: Promise<AccountState> }>();
export type AccountState = { kind: "ok"; account: CoreServiceAccountState } | { kind: "missing" } | { kind: "unknown"; reason: string };

export function accountState(c: ApiContext, ownerId: string): Promise<AccountState> {
  const name = ownerId.split(":").slice(2).join(":");
  const key = `${c.workspace}/${name}`;
  const hit = accountCache.get(key);
  if (hit && Date.now() - hit.at < 30_000) return hit.value;
  const value = api.serviceAccount(c, name).then(
    (account): AccountState => ({ kind: "ok", account }),
    (e): AccountState => (e instanceof ApiError && e.status === 404 ? { kind: "missing" } : { kind: "unknown", reason: errorMessage(e) }),
  );
  accountCache.set(key, { at: Date.now(), value });
  return value;
}

/** For tests: forget what was asked. */
export function clearAccountCache(): void {
  accountCache.clear();
}

/** "Owner disabled" / "Owner not found" next to a service-account owner; nothing otherwise. */
export function OwnerBadge({ v, owner }: { v: ViewCtx; owner: OwnerInfo | null | undefined }) {
  const [state, setState] = useState<AccountState | null>(null);
  const id = owner?.kind === "service" ? owner.id : undefined;
  useEffect(() => {
    let live = true;
    setState(null);
    if (id) void accountState(v.api, id).then((s) => live && setState(s));
    return () => {
      live = false;
    };
  }, [v.api, id]);
  if (!state || state.kind === "unknown" || (state.kind === "ok" && !state.account.disabled)) return null;
  const text = state.kind === "missing" ? "Owner not found" : "Owner disabled";
  const why =
    state.kind === "missing"
      ? "This service account no longer exists: scheduled runs fail at their start until a workspace owner chooses another owner."
      : "This service account is disabled: scheduled runs fail at their start until a workspace owner enables it or chooses another owner.";
  return (
    <span role="status" title={why} className="rounded-full border border-red-300 px-2 py-0.5 text-xs font-medium text-red-700 dark:border-red-800 dark:text-red-400">
      {text}
    </span>
  );
}

/** A workspace owner's choice of who the pipeline runs as: themselves or a service account. */
export function OwnerPicker({ v, p, onDone }: { v: ViewCtx; p: Pipeline; onDone: () => void }) {
  const accounts = useLoad(() => api.serviceAccounts(v.api), [v.api]);
  const current = p.owner?.kind === "service" ? (p.owner.id ?? "") : p.owner?.you ? "me" : "";
  const [choice, setChoice] = useState(current);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => setChoice(current), [current]);
  if (!v.canWrite || v.role !== "owner") return null;

  async function apply() {
    setBusy(true);
    setError(null);
    try {
      await api.setOwner(v.api, p.id, choice);
      onDone();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const list = accounts.state.status === "ready" ? accounts.state.data : [];
  return (
    <div role="group" aria-label="Choose who this pipeline runs as" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <select aria-label="Runs as" className={`${inputClass} max-w-sm`} value={choice} onChange={(e) => setChoice(e.target.value)}>
          {choice === "" && <option value="">{ownerLabel(p.owner)}</option>}
          <option value="me">Me</option>
          {list.map((a) => (
            <option key={a.id} value={a.id} disabled={a.disabled && a.id !== current}>
              {`Service account ${a.name} — ${a.role}${a.disabled ? " (disabled)" : ""}${a.description ? ` — ${a.description}` : ""}`}
            </option>
          ))}
        </select>
        <Button size="sm" variant="primary" disabled={busy || choice === current || choice === ""} onClick={apply}>
          {busy ? "Saving…" : "Run as this"}
        </Button>
      </div>
      {accounts.state.status === "error" && (
        <p className="text-xs text-slate-500 dark:text-slate-400">Service accounts couldn't be listed ({accounts.state.error}).</p>
      )}
      {accounts.state.status === "ready" && list.length === 0 && (
        <p className="text-xs text-slate-500 dark:text-slate-400">This workspace has no service accounts yet; a workspace owner creates them in the workspace settings.</p>
      )}
      <p className="text-xs text-slate-500 dark:text-slate-400">
        A service account's runs don't lapse when someone stops signing in; they stop at once if the account is disabled.
      </p>
      {error && <Banner tone="error">{error}</Banner>}
    </div>
  );
}

