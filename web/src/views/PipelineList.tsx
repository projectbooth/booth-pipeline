import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import { Banner, Button, EmptyState, Field, Link, Loaded, PageHeader, inputClass, linkClass, tdClass, thClass } from "../components/ui";
import type { ViewCtx } from "../context";
import { formatTime } from "../format";
import { errorMessage, useDebounced, useLoad } from "../hooks";

export function PipelineList({ v }: { v: ViewCtx }) {
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 300);
  const load = useLoad(() => api.listPipelines(v.api, dq), [v.api, dq]);
  const [creating, setCreating] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function remove(id: string, name: string) {
    if (!window.confirm(`Delete the pipeline "${name}" and all its versions? This cannot be undone.`)) return;
    setActionError(null);
    try {
      await api.deletePipeline(v.api, id);
      load.reload();
    } catch (err) {
      setActionError(errorMessage(err));
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Pipelines"
        subtitle="A Pipeline is the DAG definition — the graph of tasks you draw here. It doesn't run on its own: turn it into a Job (in the Jobs tab) to schedule or trigger it."
        actions={v.canWrite && !creating ? <Button variant="primary" onClick={() => setCreating(true)}>New pipeline</Button> : undefined}
      />
      {creating && (
        <CreatePipeline
          v={v}
          onCancel={() => setCreating(false)}
          onCreated={(id) => v.go({ name: "pipeline", id })}
        />
      )}
      <input type="search" aria-label="Search pipelines" className={`${inputClass} max-w-sm`} placeholder="Search pipelines" value={q} onChange={(e) => setQ(e.target.value)} />
      {actionError && <Banner tone="error">{actionError}</Banner>}
      <Loaded state={load.state} retry={load.reload}>
        {(page) =>
          page.items.length === 0 ? (
            <EmptyState title={dq ? "No pipelines match your search" : "No pipelines yet"}>
              {!dq && (v.canWrite ? "Create one to start drawing a DAG." : "Someone with edit access needs to create one.")}
            </EmptyState>
          ) : (
            <div className="overflow-x-auto rounded-md border border-slate-200 dark:border-slate-700">
              <table className="w-full">
                <thead className="border-b border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-900">
                  <tr>
                    <th className={thClass}>Name</th>
                    <th className={thClass}>Version</th>
                    <th className={thClass}>Updated</th>
                    <th className={thClass}>By</th>
                    <th className={thClass}>
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                  {page.items.map((p) => (
                    <tr key={p.id}>
                      <td className={tdClass}>
                        <Link href={v.href({ name: "pipeline", id: p.id })} onNavigate={v.goPath} className={`${linkClass} font-medium`}>
                          {p.name}
                        </Link>
                        {p.description && <p className="text-xs text-slate-500 dark:text-slate-400">{p.description}</p>}
                      </td>
                      <td className={tdClass}>{p.latestVersion === 0 ? "not saved yet" : `v${p.latestVersion}`}</td>
                      <td className={tdClass}>{formatTime(p.updatedAt)}</td>
                      <td className={tdClass}>{p.createdBy}</td>
                      <td className={`${tdClass} text-right`}>
                        {v.canWrite && (
                          <Button variant="danger" onClick={() => remove(p.id, p.name)} aria-label={`Delete ${p.name}`}>
                            Delete
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </Loaded>
    </div>
  );
}

function CreatePipeline({ v, onCancel, onCreated }: { v: ViewCtx; onCancel: () => void; onCreated: (id: string) => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; field?: string } | null>(null);

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const p = await api.createPipeline(v.api, name.trim(), description.trim());
      onCreated(p.id);
    } catch (err) {
      setError({ message: errorMessage(err), field: (err as { field?: string }).field });
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} aria-label="New pipeline" className="flex max-w-lg flex-col gap-3 rounded-md border border-slate-200 p-4 dark:border-slate-700">
      <Field id="new-name" label="Name" required error={error?.field === "name" ? error.message : undefined}>
        {(p) => <input {...p} className={inputClass} value={name} maxLength={200} autoFocus onChange={(e) => setName(e.target.value)} />}
      </Field>
      <Field id="new-desc" label="Description">
        {(p) => <input {...p} className={inputClass} value={description} maxLength={4000} onChange={(e) => setDescription(e.target.value)} />}
      </Field>
      {error && error.field !== "name" && <Banner tone="error">{error.message}</Banner>}
      <div className="flex gap-2">
        <Button type="submit" variant="primary" disabled={busy || name.trim() === ""}>
          {busy ? "Creating…" : "Create and open the builder"}
        </Button>
        <Button onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
