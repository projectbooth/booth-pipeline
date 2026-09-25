import { useEffect, useState } from "react";
import { api, type ApiContext } from "../api/client";
import { errorMessage, useDebounced } from "../hooks";
import type { TaskEntity } from "../types";
import { Banner, Button, inputClass } from "./ui";

export interface TaskPickerProps {
  api: ApiContext;
  onPick: (task: TaskEntity) => void;
  onCancel?: () => void;
  autoFocus?: boolean;
}

/** Search existing tasks, or create a new one — shared between the canvas's "+ Add task" (picking
 *  fresh) and `ReferencePanel`'s "Change" (repointing an existing node). Unconfigured tasks (no
 *  saved version AND no draft — ADR 0073) are hidden by default: referencing one reproduces "has
 *  no saved version or draft yet" the moment the pipeline is saved, and most of the time an
 *  unconfigured task sitting in the library is a throwaway orphan left behind by an earlier pick,
 *  not a deliberate choice. A toggle reveals them for the real case — picking a task someone else
 *  is still mid-configuring. */
export function TaskPicker({ api: apiCtx, onPick, onCancel, autoFocus = true }: TaskPickerProps) {
  const [query, setQuery] = useState("");
  const debounced = useDebounced(query, 250);
  const [matches, setMatches] = useState<TaskEntity[]>([]);
  const [showUnconfigured, setShowUnconfigured] = useState(false);
  const [creating, setCreating] = useState(false);
  const [pickError, setPickError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api.listTasks(apiCtx, debounced).then(
      (page) => alive && setMatches(page.items),
      () => alive && setMatches([]),
    );
    return () => {
      alive = false;
    };
  }, [apiCtx, debounced]);

  async function createAndPick(name: string) {
    setCreating(true);
    setPickError(null);
    try {
      const t = await api.createTask(apiCtx, name, "");
      onPick(t);
    } catch (err) {
      setPickError(errorMessage(err));
    } finally {
      setCreating(false);
    }
  }

  // "Configured" means real, runnable code exists (ADR 0073): either a saved version, or a draft
  // that was never promoted to one — a task can be fully usable via its draft alone.
  const configured = matches.filter((t) => t.latestVersion > 0 || t.hasDraft);
  const unconfigured = matches.filter((t) => t.latestVersion === 0 && !t.hasDraft);
  const visible = showUnconfigured ? matches : configured;

  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-slate-200 p-2 dark:border-slate-700">
      <input
        autoFocus={autoFocus}
        className={inputClass}
        placeholder="Search tasks, or type a name to create one"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      <div className="flex max-h-56 flex-col gap-0.5 overflow-auto">
        {visible.map((t) => (
          <button
            key={t.id}
            type="button"
            className="rounded px-2 py-1 text-left text-sm text-slate-700 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
            onClick={() => onPick(t)}
          >
            <span className="inline-flex items-center gap-1.5">
              <span>{t.name}</span>
              {t.latestVersion === 0 && !t.hasDraft && (
                <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium leading-none text-amber-800 dark:bg-amber-950 dark:text-amber-300">
                  no saved version yet
                </span>
              )}
            </span>
            {t.description && <span className="ml-1 text-xs text-slate-500 dark:text-slate-400">— {t.description}</span>}
          </button>
        ))}
        {visible.length === 0 && (
          <p className="px-2 py-1 text-xs text-slate-500 dark:text-slate-400">
            {matches.length === 0 ? "No matching tasks." : "No configured tasks match — try the toggle below."}
          </p>
        )}
      </div>
      {!showUnconfigured && unconfigured.length > 0 && (
        <button
          type="button"
          className="self-start text-xs text-indigo-600 hover:underline dark:text-indigo-400"
          onClick={() => setShowUnconfigured(true)}
        >
          Show {unconfigured.length} unconfigured task{unconfigured.length === 1 ? "" : "s"} too
        </button>
      )}
      {query.trim() !== "" && (
        <Button onClick={() => createAndPick(query.trim())} disabled={creating}>
          {creating ? "Creating…" : `+ Create new task "${query.trim()}"`}
        </Button>
      )}
      {pickError && <Banner tone="error">{pickError}</Banner>}
      {onCancel && <Button onClick={onCancel}>Cancel</Button>}
    </div>
  );
}
