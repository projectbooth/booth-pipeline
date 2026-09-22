import { useEffect, useState } from "react";
import { api, type ApiContext } from "../api/client";
import { errorMessage, useDebounced } from "../hooks";
import type { CatalogCodeEntry, CatalogVersionSummary, TaskCode } from "../types";
import { Banner, Button, inputClass } from "./ui";

// Picks a code-catalog entry and a version for a Task (ADR 0010's narrow reference: {entryId,
// version}). Browse-only: the pipeline BACKEND resolves and snapshots the chosen version at save
// time, so what is stored is immutable code, never a live pointer.
//
// This must never break the builder. Inline code needs no catalog at all, so when the catalog
// is not installed or unreachable the picker says so next to itself and everything else keeps
// working.

type CatalogRef = Extract<TaskCode, { type: "catalog" }>;

const LATEST = "latest";

export function CodePicker({ api: ctx, value, onChange, disabled }: { api: ApiContext; value: CatalogRef | null; onChange: (c: CatalogRef) => void; disabled?: boolean }) {
  const [query, setQuery] = useState("");
  const debounced = useDebounced(query, 300);
  const [entries, setEntries] = useState<CatalogCodeEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [versions, setVersions] = useState<CatalogVersionSummary[]>([]);

  useEffect(() => {
    if (disabled) return;
    let alive = true;
    api.catalogCode(ctx, debounced).then(
      (page) => alive && (setEntries(page.items), setError(null)),
      (err: unknown) => alive && (setEntries(null), setError(errorMessage(err))),
    );
    return () => {
      alive = false;
    };
  }, [ctx, debounced, disabled]);

  const entryId = value?.entryId;
  useEffect(() => {
    if (!entryId) return;
    let alive = true;
    api.catalogVersions(ctx, entryId).then(
      (page) => alive && setVersions(page.items),
      () => alive && setVersions([]),
    );
    return () => {
      alive = false;
    };
  }, [ctx, entryId]);

  function pick(e: CatalogCodeEntry) {
    // A different entry: everything previously snapshotted belongs to the old one, so drop it —
    // the server re-resolves the reference on save.
    onChange({ type: "catalog", entryId: e.id, version: LATEST, name: e.name, language: e.language });
  }

  function setVersion(version: string) {
    if (!value) return;
    onChange({ type: "catalog", entryId: value.entryId, version, name: value.name, language: value.language });
  }

  return (
    <div className="flex flex-col gap-2">
      {error && (
        <Banner tone="warn">The code catalog could not be reached ({error}). Pick a storage reference instead, or try again shortly.</Banner>
      )}
      {value && (
        <div className="rounded-md border border-slate-200 p-2 text-sm dark:border-slate-700">
          <p className="font-medium text-slate-900 dark:text-slate-100">{value.name ?? value.entryId}</p>
          <div className="mt-1 flex items-center gap-2">
            <label htmlFor="catalog-version" className="text-xs text-slate-500 dark:text-slate-400">
              Version
            </label>
            <select
              id="catalog-version"
              className={`${inputClass} w-auto`}
              value={value.version}
              disabled={disabled}
              onChange={(e) => setVersion(e.target.value)}
            >
              <option value={LATEST}>latest (pinned when you save)</option>
              {versions.map((v) => (
                <option key={v.version} value={v.version}>
                  {v.version}
                </option>
              ))}
              {value.version !== LATEST && !versions.some((v) => v.version === value.version) && <option value={value.version}>{value.version}</option>}
            </select>
          </div>
          {value.sha256 && (
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
              Saved snapshot: <span className="font-mono">{value.sha256.slice(0, 12)}…</span> — a run uses exactly this code, whatever the catalog holds later.
            </p>
          )}
        </div>
      )}
      {!disabled && (
        <>
          <input
            type="search"
            aria-label="Search the code catalog"
            className={inputClass}
            placeholder="Search Python code in the catalog"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {entries && entries.length === 0 && <p className="text-xs text-slate-500 dark:text-slate-400">No Python code entries match.</p>}
          {entries && entries.length > 0 && (
            <ul className="max-h-40 divide-y divide-slate-200 overflow-auto rounded-md border border-slate-200 dark:divide-slate-700 dark:border-slate-700">
              {entries.map((e) => (
                <li key={e.id} className="flex items-center justify-between gap-2 px-2 py-1.5">
                  <span className="min-w-0">
                    <span className="block truncate text-sm text-slate-900 dark:text-slate-100">{e.name}</span>
                    <span className="block truncate text-xs text-slate-500 dark:text-slate-400">
                      {e.description || "No description"} · {e.versionCount} version{e.versionCount === 1 ? "" : "s"}
                    </span>
                  </span>
                  <Button onClick={() => pick(e)} aria-label={`Use ${e.name}`}>
                    Use
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
