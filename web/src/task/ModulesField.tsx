import { useEffect, useState } from "react";
import { api, type ApiContext } from "../api/client";
import { errorMessage, useDebounced } from "../hooks";
import type { CatalogCodeEntry, CatalogVersionSummary, ModuleRef } from "../types";
import { Button, inputClass, inputInlineClass, monoClass } from "../ui/primitives";

// Shared modules from the code catalog: Python files a task imports by name (`import market_calendar`).
// Like the task's own catalog code, each is pinned and snapshotted by the server when the task is saved,
// so a new version in the catalog never changes a saved task; choose a newer version here to move on.

const LATEST = "latest";

/** A server error about modules: the list ("modules") or one entry ("modules[1].name"). */
export function isModulesField(field: string | null | undefined): boolean {
  return field === "modules" || /^modules\[\d+\]\.(name|entryId|version)$/.test(field ?? "");
}

/** What a task imports a catalog entry as, from the entry's name: "Market calendar" -> market_calendar. */
export function importName(entryName: string): string {
  const name = entryName
    .trim()
    .toLowerCase()
    .replace(/\.py$/, "")
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return /^[0-9]/.test(name) ? `m_${name}` : name || "module";
}

export interface ModulesFieldProps {
  api: ApiContext;
  value: ModuleRef[];
  error?: { message: string; field?: string | null };
  onChange: (modules: ModuleRef[]) => void;
}

export function ModulesField({ api: ctx, value, error, onChange }: ModulesFieldProps) {
  const [adding, setAdding] = useState(false);
  const rowError = (i: number) =>
    error?.field && error.field.startsWith(`modules[${i}].`) ? error.message : undefined;
  const set = (i: number, next: ModuleRef) => onChange(value.map((m, j) => (j === i ? next : m)));

  return (
    <section aria-label="Shared modules" className="flex flex-col gap-1.5">
      <h3 className="text-xs font-medium text-slate-600 dark:text-slate-300">Shared modules</h3>
      <p className="text-xs text-slate-500 dark:text-slate-400">
        Python files from the code catalog that this task imports by name. Each is pinned to a version when you save, so the task never changes
        by itself. At most 256 KiB together.
      </p>
      {value.length > 0 && (
        <ul aria-label="Modules this task imports" className="flex flex-col gap-2">
          {value.map((m, i) => (
            <ModuleRow key={i} api={ctx} index={i} m={m} error={rowError(i)} onChange={(next) => set(i, next)} onRemove={() => onChange(value.filter((_, j) => j !== i))} />
          ))}
        </ul>
      )}
      {error?.field === "modules" && (
        <p role="alert" className="text-xs text-red-600 dark:text-red-400">
          {error.message}
        </p>
      )}
      {adding ? (
        <ModuleSearch
          api={ctx}
          onPick={(e) => {
            onChange([...value, { name: importName(e.name), entryId: e.id, version: LATEST, entryName: e.name }]);
            setAdding(false);
          }}
          onCancel={() => setAdding(false)}
        />
      ) : (
        <div>
          <Button size="sm" onClick={() => setAdding(true)}>
            Add a module
          </Button>
        </div>
      )}
    </section>
  );
}

function ModuleRow({
  api: ctx,
  index,
  m,
  error,
  onChange,
  onRemove,
}: {
  api: ApiContext;
  index: number;
  m: ModuleRef;
  error?: string;
  onChange: (m: ModuleRef) => void;
  onRemove: () => void;
}) {
  const [versions, setVersions] = useState<CatalogVersionSummary[]>([]);
  useEffect(() => {
    let alive = true;
    api.catalogVersions(ctx, m.entryId).then(
      (list) => alive && setVersions(list),
      () => alive && setVersions([]),
    );
    return () => {
      alive = false;
    };
  }, [ctx, m.entryId]);

  return (
    <li className="flex flex-col gap-1 rounded-md border border-slate-200 p-2 dark:border-slate-700">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-xs text-slate-500 dark:text-slate-400">import</span>
        <label className="sr-only" htmlFor={`module-${index}-name`}>
          Import name for {m.entryName ?? m.entryId}
        </label>
        <input
          id={`module-${index}-name`}
          className={`${inputClass} ${monoClass} w-48`}
          value={m.name}
          spellCheck={false}
          onChange={(e) => onChange({ ...m, name: e.target.value })}
        />
        <span className="text-xs text-slate-500 dark:text-slate-400">from {m.entryName ?? m.entryId}</span>
        <label className="sr-only" htmlFor={`module-${index}-version`}>
          Version of {m.entryName ?? m.entryId}
        </label>
        <select
          id={`module-${index}-version`}
          className={inputInlineClass}
          value={m.version}
          // A different version is a different snapshot: drop the stored one, the server fetches it on save.
          onChange={(e) => onChange({ name: m.name, entryId: m.entryId, version: e.target.value, entryName: m.entryName })}
        >
          <option value={LATEST}>latest (pinned when you save)</option>
          {versions.map((v) => (
            <option key={v.version} value={v.version}>
              {v.version}
            </option>
          ))}
          {m.version !== LATEST && !versions.some((v) => v.version === m.version) && <option value={m.version}>{m.version}</option>}
        </select>
        <Button size="sm" variant="ghost" aria-label={`Remove ${m.name}`} onClick={onRemove}>
          Remove
        </Button>
      </div>
      {m.sha256 && (
        <p className="text-xs text-slate-500 dark:text-slate-400">
          Saved snapshot <span className="font-mono">{m.sha256.slice(0, 12)}…</span>: runs use exactly this, whatever the catalog holds later.
        </p>
      )}
      {error && (
        <p role="alert" className="text-xs text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
    </li>
  );
}

function ModuleSearch({ api: ctx, onPick, onCancel }: { api: ApiContext; onPick: (e: CatalogCodeEntry) => void; onCancel: () => void }) {
  const [query, setQuery] = useState("");
  const debounced = useDebounced(query, 300);
  const [entries, setEntries] = useState<CatalogCodeEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    api.catalogCode(ctx, debounced).then(
      (page) => alive && (setEntries(page.items), setLoadError(null)),
      (err: unknown) => alive && (setEntries(null), setLoadError(errorMessage(err))),
    );
    return () => {
      alive = false;
    };
  }, [ctx, debounced]);

  return (
    <div className="flex flex-col gap-2 rounded-md border border-slate-200 p-2 dark:border-slate-700">
      <div className="flex gap-2">
        <input
          type="search"
          aria-label="Search the code catalog for a module"
          className={inputClass}
          placeholder="Search Python code in the catalog"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <Button size="sm" onClick={onCancel}>
          Cancel
        </Button>
      </div>
      {loadError && <p className="text-xs text-amber-700 dark:text-amber-400">The code catalog could not be reached ({loadError}).</p>}
      {entries && entries.length === 0 && <p className="text-xs text-slate-500 dark:text-slate-400">No Python code entries match.</p>}
      {entries && entries.length > 0 && (
        <ul className="max-h-40 divide-y divide-slate-200 overflow-auto rounded-md border border-slate-200 dark:divide-slate-700 dark:border-slate-700">
          {entries.map((e) => (
            <li key={e.id} className="flex items-center justify-between gap-2 px-2 py-1.5">
              <span className="min-w-0">
                <span className="block truncate text-sm text-slate-900 dark:text-slate-100">{e.name}</span>
                <span className="block truncate text-xs text-slate-500 dark:text-slate-400">{e.description || "No description"}</span>
              </span>
              <Button size="sm" onClick={() => onPick(e)} aria-label={`Import ${e.name}`}>
                Import
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
