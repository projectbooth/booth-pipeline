import { useEffect, useState } from "react";
import { api, type ApiContext } from "../api/client";
import { errorMessage, useDebounced } from "../hooks";
import type { StorageBackendSummary, TaskCode } from "../types";
import { Banner, Button, inputClass } from "./ui";

// Picks a booth-storage object and backend for a Task (ADR 0063's narrow reference: {backendId,
// path}). Browse-only, same contract as CodePicker: the pipeline BACKEND resolves and snapshots
// the chosen object at save time, so what is stored is immutable code, never a live pointer.
//
// booth-storage's own file browser is not reusable here — its published package
// (@projectbooth/storage-ui) only exports full page-level apps (StorageApp / StorageBrowseApp /
// StorageAdminApp), not a picker component — so this is a thin widget over its REST API directly,
// the same pattern CodePicker already uses for the catalog.
//
// This must never break the builder. A failure here is shown next to the picker, and everything
// else keeps working — a storage-backed task just cannot be picked until it is reachable.

type StorageRef = Extract<TaskCode, { type: "storage" }>;

export function StoragePicker({ api: ctx, value, onChange, disabled }: { api: ApiContext; value: StorageRef | null; onChange: (c: StorageRef) => void; disabled?: boolean }) {
  const [backends, setBackends] = useState<StorageBackendSummary[] | null>(null);
  const [backendError, setBackendError] = useState<string | null>(null);
  const [backendId, setBackendId] = useState(value?.backendId ?? "");
  const [prefix, setPrefix] = useState("");
  const debounced = useDebounced(prefix, 300);
  const [entries, setEntries] = useState<{ path: string }[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);

  useEffect(() => {
    if (disabled) return;
    let alive = true;
    api.storageBackends(ctx).then(
      (list) => alive && (setBackends(list), setBackendError(null), setBackendId((b) => b || list[0]?.id || "")),
      (err: unknown) => alive && (setBackends(null), setBackendError(errorMessage(err))),
    );
    return () => {
      alive = false;
    };
  }, [ctx, disabled]);

  useEffect(() => {
    if (disabled || !backendId) return;
    let alive = true;
    api.storageObjects(ctx, backendId, debounced).then(
      (page) => alive && (setEntries(page.entries), setListError(null)),
      (err: unknown) => alive && (setEntries(null), setListError(errorMessage(err))),
    );
    return () => {
      alive = false;
    };
  }, [ctx, backendId, debounced, disabled]);

  function pick(path: string) {
    // A different object: everything previously snapshotted belongs to the old one, so drop it —
    // the server re-resolves the reference on save.
    onChange({ type: "storage", backendId, path });
  }

  return (
    <div className="flex flex-col gap-2">
      {backendError && (
        <Banner tone="warn">
          Storage backends could not be listed ({backendError}). Pick a catalog reference instead, or try again shortly.
        </Banner>
      )}
      {value && (
        <div className="rounded-md border border-slate-200 p-2 text-sm dark:border-slate-700">
          <p className="font-medium text-slate-900 dark:text-slate-100">{value.name ?? value.path}</p>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            {value.backendId} · <span className="font-mono">{value.path}</span>
          </p>
          {value.sha256 && (
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
              Saved snapshot: <span className="font-mono">{value.sha256.slice(0, 12)}…</span> — a run uses exactly this code, whatever is at that path later.
            </p>
          )}
        </div>
      )}
      {!disabled && backends && backends.length > 0 && (
        <>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-slate-600 dark:text-slate-300">Backend</span>
            <select className={`${inputClass} w-auto`} value={backendId} onChange={(e) => (setBackendId(e.target.value), setEntries(null))}>
              {backends.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.displayName || b.id}
                </option>
              ))}
            </select>
          </label>
          <input
            type="search"
            aria-label="Filter by path prefix"
            className={inputClass}
            placeholder="Filter by path (e.g. tasks/)"
            value={prefix}
            onChange={(e) => setPrefix(e.target.value)}
          />
          {listError && <Banner tone="warn">Objects could not be listed ({listError}).</Banner>}
          {entries && entries.length === 0 && <p className="text-xs text-slate-500 dark:text-slate-400">No objects match.</p>}
          {entries && entries.length > 0 && (
            <ul className="max-h-40 divide-y divide-slate-200 overflow-auto rounded-md border border-slate-200 dark:divide-slate-700 dark:border-slate-700">
              {entries.map((o) => (
                <li key={o.path} className="flex items-center justify-between gap-2 px-2 py-1.5">
                  <span className="min-w-0 truncate font-mono text-xs text-slate-900 dark:text-slate-100">{o.path}</span>
                  <Button onClick={() => pick(o.path)} aria-label={`Use ${o.path}`}>
                    Use
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      {!disabled && backends && backends.length === 0 && <p className="text-xs text-slate-500 dark:text-slate-400">No storage backends are configured.</p>}
    </div>
  );
}
