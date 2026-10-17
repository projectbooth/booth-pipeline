import { useState } from "react";
import { api } from "../../api/client";
import type { ViewCtx } from "../../context";
import { errorMessage } from "../../hooks";
import type { OwnerInfo, Pipeline } from "../../types";
import { Banner, Button } from "../../ui/primitives";

// Who a pipeline's scheduled runs act as (ADR 0119 amendment 3, ADR 0120). The backend enforces
// every rule; this only offers what the caller may do:
// - an editor or owner may Take over a person-owned pipeline (become its owner);
// - only a workspace owner may take one over from a service account;
// - choosing a service account waits for core's account list (the picker isn't here yet).

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
