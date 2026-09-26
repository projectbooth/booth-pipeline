import { useEffect, useState } from "react";
import { api, type ApiContext } from "../api/client";
import { errorMessage, useDebounced } from "../hooks";
import type { TaskEntity } from "../types";
import { Badge, Banner, Button, inputClass } from "../ui/primitives";

export interface TaskPickerProps {
  api: ApiContext;
  onPick: (task: TaskEntity) => void;
  onCancel?: () => void;
  autoFocus?: boolean;
  /** What picking does, for the heading ("Add a task" / "Point this node at a different task"). */
  title: string;
}

/** Search existing tasks, or create a new one by name — shared by "+ Add task" and a node's
 *  "Change". Picking an existing task NEVER creates a Task (the orphan-task bug the old builder
 *  had: every "+ Add task" click silently created an empty `task_N`).
 *
 *  Unconfigured tasks (no saved version AND no draft — ADR 0073) are hidden by default: pointing
 *  a node at one reproduces "has no saved version yet" the moment the pipeline is saved, and most
 *  of the time such a task is a leftover, not a deliberate choice. A toggle reveals them, badged,
 *  for the real case of picking a task someone is still configuring. */
export function TaskPicker({ api: ctx, onPick, onCancel, autoFocus = true, title }: TaskPickerProps) {
  const [query, setQuery] = useState("");
  const debounced = useDebounced(query.trim(), 250);
  const [matches, setMatches] = useState<TaskEntity[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [showUnconfigured, setShowUnconfigured] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api.listTasks(ctx, debounced).then(
      (page) => alive && (setMatches(page.items), setListError(null)),
      (err: unknown) => alive && (setMatches([]), setListError(errorMessage(err))),
    );
    return () => {
      alive = false;
    };
  }, [ctx, debounced]);

  async function createAndPick(name: string) {
    setCreating(true);
    setCreateError(null);
    try {
      onPick(await api.createTask(ctx, name, ""));
    } catch (err) {
      setCreateError(errorMessage(err));
    } finally {
      setCreating(false);
    }
  }

  const all = matches ?? [];
  const configured = all.filter((t) => t.latestVersion > 0 || t.hasDraft);
  const unconfigured = all.filter((t) => t.latestVersion === 0 && !t.hasDraft);
  const visible = showUnconfigured ? all : configured;
  const name = query.trim();
  const exact = all.some((t) => t.name === name);

  return (
    <section aria-label={title} className="flex flex-col gap-2 rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">{title}</h3>
        {onCancel && (
          <Button size="sm" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        )}
      </div>
      <input
        type="search"
        aria-label="Search tasks"
        autoFocus={autoFocus}
        className={inputClass}
        placeholder="Search tasks, or type a new name to create one"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {listError && <Banner tone="error">Could not list tasks: {listError}</Banner>}
      <ul aria-label="Matching tasks" className="flex max-h-64 flex-col divide-y divide-slate-100 overflow-auto rounded-md border border-slate-200 dark:divide-slate-800 dark:border-slate-700">
        {visible.map((t) => (
          <li key={t.id}>
            <button
              type="button"
              onClick={() => onPick(t)}
              className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-slate-50 dark:hover:bg-slate-800"
            >
              <span className="min-w-0">
                <span className="block truncate font-mono text-sm text-slate-900 dark:text-slate-100">{t.name}</span>
                {t.description && <span className="block truncate text-xs text-slate-500 dark:text-slate-400">{t.description}</span>}
              </span>
              {t.latestVersion > 0 ? <Badge tone="indigo">v{t.latestVersion}</Badge> : t.hasDraft ? <Badge tone="sky">Draft only</Badge> : <Badge tone="amber">Not configured yet</Badge>}
            </button>
          </li>
        ))}
        {matches !== null && visible.length === 0 && (
          <li className="px-3 py-2 text-xs text-slate-500 dark:text-slate-400">
            {all.length === 0 ? (name ? "No tasks match." : "No tasks yet.") : "No configured tasks match. Unconfigured ones are hidden below."}
          </li>
        )}
      </ul>
      {!showUnconfigured && unconfigured.length > 0 && (
        <button type="button" className="self-start text-xs text-indigo-600 hover:underline dark:text-indigo-400" onClick={() => setShowUnconfigured(true)}>
          Show {unconfigured.length} unconfigured task{unconfigured.length === 1 ? "" : "s"} too
        </button>
      )}
      {name && !exact && (
        <div>
          <Button onClick={() => createAndPick(name)} disabled={creating}>
            {creating ? "Creating…" : `+ Create task "${name}"`}
          </Button>
        </div>
      )}
      {createError && <Banner tone="error">{createError}</Banner>}
    </section>
  );
}
