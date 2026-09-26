import { Fragment, useState, type FormEvent } from "react";
import { api } from "../api/client";
import type { ViewCtx } from "../context";
import { formatRelative, formatStamp, formatTime } from "../format";
import { errorMessage, useDebounced, useLoad } from "../hooks";
import type { TaskEntity } from "../types";
import {
  Badge,
  Banner,
  Button,
  Card,
  EmptyState,
  Field,
  InlineConfirm,
  Link,
  Loaded,
  OverflowMenu,
  PageHeader,
  Skeleton,
  inputClass,
  rowHoverClass,
  tableClass,
  tbodyClass,
  tdClass,
  thClass,
  theadClass,
} from "../ui/primitives";

// S4 — the Tasks list (docs/decisions/0016 §4, Figma screen 2). One row per task entity (the
// dedup); a task's version history lives on the task itself (S5c). The Figma's Type / Status /
// Duration / Last run / Cluster columns are dropped: the backend has no per-task run data, and a
// task's language needs its config, which this list doesn't load (§3, ruled Q1).

/** Where a task stands, from the entity alone. Whether a draft actually *differs* from the latest
 *  version needs both configs loaded, so that finer distinction is shown on the task page only. */
export function TaskVersionBadge({ t }: { t: TaskEntity }) {
  if (t.latestVersion > 0) return <Badge tone="indigo">v{t.latestVersion}</Badge>;
  if (t.hasDraft) return <Badge tone="sky" title="Saved with plain Save, never as a numbered version">Draft only</Badge>;
  return <Badge tone="amber">Not configured yet</Badge>;
}

export function TaskList({ v }: { v: ViewCtx }) {
  const [query, setQuery] = useState("");
  const q = useDebounced(query.trim(), 250);
  const [creating, setCreating] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const load = useLoad(() => api.listTasks(v.api, q), [v.api, q]);

  async function remove(t: TaskEntity) {
    setDeleting(true);
    setActionError(null);
    try {
      await api.deleteTask(v.api, t.id);
      setConfirmDelete(null);
      load.reload();
    } catch (err) {
      setActionError(`Could not delete "${t.name}": ${errorMessage(err)}`);
      setConfirmDelete(null);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Tasks"
        subtitle="Reusable, versioned units of code. Pipelines reference them."
        actions={
          v.canWrite && !creating ? (
            <Button variant="primary" onClick={() => setCreating(true)}>
              + New task
            </Button>
          ) : undefined
        }
      />

      {creating && <NewTaskCard v={v} onCancel={() => setCreating(false)} />}

      <input type="search" aria-label="Search tasks" placeholder="Search tasks…" className={`${inputClass} max-w-sm`} value={query} onChange={(e) => setQuery(e.target.value)} />

      {actionError && <Banner tone="error">{actionError}</Banner>}

      <Loaded
        state={load.state}
        retry={load.reload}
        skeleton={
          <div role="status" aria-label="Loading" className="flex flex-col gap-3">
            {Array.from({ length: 6 }, (_, i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        }
      >
        {(page) =>
          page.items.length === 0 ? (
            q ? (
              <EmptyState title={`No tasks match "${q}"`} />
            ) : (
              <EmptyState title="No tasks yet">{v.canWrite ? "Create one here, or add one straight from a pipeline's canvas." : "Someone with edit access needs to create one."}</EmptyState>
            )
          ) : (
            <div className="overflow-x-auto">
              <table className={tableClass}>
                <thead className={theadClass}>
                  <tr>
                    <th className={thClass}>Name</th>
                    <th className={thClass}>Description</th>
                    <th className={thClass}>Version</th>
                    <th className={thClass}>Updated</th>
                    <th className={thClass}>Created by</th>
                    <th className={thClass}>
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody className={tbodyClass}>
                  {page.items.map((t) => {
                    const href = v.href({ name: "task", id: t.id });
                    const updated = t.draftUpdatedAt && t.draftUpdatedAt > t.updatedAt ? t.draftUpdatedAt : t.updatedAt;
                    return (
                      <Fragment key={t.id}>
                        <tr className={`cursor-pointer ${rowHoverClass}`} onClick={(e) => !(e.target as HTMLElement).closest("a,button") && v.goPath(href)}>
                          <td className={tdClass}>
                            <Link href={href} onNavigate={v.goPath} className="font-mono text-sm font-medium text-slate-900 hover:underline dark:text-slate-50">
                              {t.name}
                            </Link>
                          </td>
                          <td className={`${tdClass} max-w-md truncate text-slate-600 dark:text-slate-300`}>{t.description || <span className="text-slate-400">—</span>}</td>
                          <td className={tdClass}>
                            <TaskVersionBadge t={t} />
                          </td>
                          <td className={`${tdClass} whitespace-nowrap font-mono text-xs`} title={`${formatTime(updated)} (${formatRelative(updated)})`}>
                            {formatStamp(updated)}
                          </td>
                          <td className={`${tdClass} text-slate-600 dark:text-slate-300`}>{t.createdBy}</td>
                          <td className={`${tdClass} text-right`}>
                            {v.canWrite && <OverflowMenu label={`More actions for ${t.name}`} items={[{ label: "Delete task…", danger: true, onSelect: () => setConfirmDelete(t.id) }]} />}
                          </td>
                        </tr>
                        {confirmDelete === t.id && (
                          <tr>
                            <td colSpan={6} className="px-3 py-2">
                              <InlineConfirm
                                message={
                                  <>
                                    Delete task <strong className="font-mono">{t.name}</strong> and all its versions? A task still referenced by a saved pipeline version can't be deleted.
                                  </>
                                }
                                confirmLabel="Delete task"
                                busyLabel="Deleting…"
                                busy={deleting}
                                onConfirm={() => remove(t)}
                                onCancel={() => setConfirmDelete(null)}
                              />
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )
        }
      </Loaded>
    </div>
  );
}

function NewTaskCard({ v, onCancel }: { v: ViewCtx; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; field?: string } | null>(null);

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const t = await api.createTask(v.api, name.trim(), description.trim());
      v.go({ name: "task", id: t.id });
    } catch (err) {
      setError({ message: errorMessage(err), field: (err as { field?: string }).field });
      setBusy(false);
    }
  }

  return (
    <Card title="New task">
      <form onSubmit={submit} aria-label="New task" className="flex flex-col gap-3 p-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field id="new-task-name" label="Name" required error={error?.field === "name" ? error.message : undefined}>
            {(p) => <input {...p} autoFocus className={`${inputClass} font-mono`} value={name} maxLength={100} onChange={(e) => setName(e.target.value)} />}
          </Field>
          <Field id="new-task-description" label="Description">
            {(p) => <input {...p} className={inputClass} value={description} maxLength={500} onChange={(e) => setDescription(e.target.value)} />}
          </Field>
        </div>
        {error && error.field !== "name" && <Banner tone="error">{error.message}</Banner>}
        <div className="flex gap-2">
          <Button type="submit" variant="primary" disabled={busy || name.trim() === ""}>
            {busy ? "Creating…" : "Create and configure"}
          </Button>
          <Button onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        </div>
      </form>
    </Card>
  );
}
