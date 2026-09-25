import { useState, type FormEvent } from "react";
import { api, ApiError } from "../api/client";
import { TaskConfigForm } from "../components/TaskConfigForm";
import { Banner, Button, EmptyState, Field, Link, Loaded, PageHeader, inputClass, linkClass, tdClass, thClass } from "../components/ui";
import type { ViewCtx } from "../context";
import { formatTime } from "../format";
import { errorMessage, useDebounced, useLoad } from "../hooks";
import type { TaskConfig } from "../types";

// The Task library (ADR 0071): browse, create, configure and version standalone tasks —
// independent of any pipeline that references them. A task's own code/runner/retry/params/timeout
// live here, versioned separately from the pipelines that pick a specific version of it.

const DEFAULT_CONFIG: TaskConfig = {
  code: { type: "catalog", entryId: "", version: "latest" },
  runner: "base",
  retry: null,
  params: {},
  timeoutSeconds: 3600,
  platformAccess: false,
};

export function TaskList({ v }: { v: ViewCtx }) {
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 300);
  const load = useLoad(() => api.listTasks(v.api, dq), [v.api, dq]);
  const [creating, setCreating] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function remove(id: string, name: string) {
    if (!window.confirm(`Delete the task "${name}"? This cannot be undone.`)) return;
    setActionError(null);
    try {
      await api.deleteTask(v.api, id);
      load.reload();
    } catch (err) {
      setActionError(errorMessage(err));
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Tasks"
        subtitle="A Task is a standalone, versioned unit of code — reusable across any number of pipelines. A pipeline's DAG references a task at a specific version; editing a task here never retroactively changes a pipeline that already points at an older version."
        actions={v.canWrite && !creating ? <Button variant="primary" onClick={() => setCreating(true)}>New task</Button> : undefined}
      />
      {creating && <CreateTask v={v} onCancel={() => setCreating(false)} onCreated={(id) => v.go({ name: "task", id })} />}
      <input type="search" aria-label="Search tasks" className={`${inputClass} max-w-sm`} placeholder="Search tasks" value={q} onChange={(e) => setQ(e.target.value)} />
      {actionError && <Banner tone="error">{actionError}</Banner>}
      <Loaded state={load.state} retry={load.reload}>
        {(page) =>
          page.items.length === 0 ? (
            <EmptyState title={dq ? "No tasks match your search" : "No tasks yet"}>
              {!dq && (v.canWrite ? "Create one, or add one straight from a pipeline's canvas." : "Someone with edit access needs to create one.")}
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
                  {page.items.map((t) => (
                    <tr key={t.id}>
                      <td className={tdClass}>
                        <Link href={v.href({ name: "task", id: t.id })} onNavigate={v.goPath} className={`${linkClass} font-medium`}>
                          {t.name}
                        </Link>
                        {t.description && <p className="text-xs text-slate-500 dark:text-slate-400">{t.description}</p>}
                      </td>
                      <td className={tdClass}>{t.latestVersion === 0 ? (t.hasDraft ? "draft only, no saved version" : "not configured yet") : `v${t.latestVersion}`}</td>
                      <td className={tdClass}>{formatTime(t.updatedAt)}</td>
                      <td className={tdClass}>{t.createdBy}</td>
                      <td className={`${tdClass} text-right`}>
                        {v.canWrite && (
                          <Button variant="danger" onClick={() => remove(t.id, t.name)} aria-label={`Delete ${t.name}`}>
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

function CreateTask({ v, onCancel, onCreated }: { v: ViewCtx; onCancel: () => void; onCreated: (id: string) => void }) {
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
      onCreated(t.id);
    } catch (err) {
      setError({ message: errorMessage(err), field: (err as { field?: string }).field });
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} aria-label="New task" className="flex max-w-lg flex-col gap-3 rounded-md border border-slate-200 p-4 dark:border-slate-700">
      <Field id="new-task-name" label="Name" required error={error?.field === "name" ? error.message : undefined}>
        {(p) => <input {...p} className={inputClass} value={name} maxLength={200} autoFocus onChange={(e) => setName(e.target.value)} />}
      </Field>
      <Field id="new-task-desc" label="Description">
        {(p) => <input {...p} className={inputClass} value={description} maxLength={4000} onChange={(e) => setDescription(e.target.value)} />}
      </Field>
      {error && error.field !== "name" && <Banner tone="error">{error.message}</Banner>}
      <div className="flex gap-2">
        <Button type="submit" variant="primary" disabled={busy || name.trim() === ""}>
          {busy ? "Creating…" : "Create"}
        </Button>
        <Button onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

export function TaskDetail({ v, taskId }: { v: ViewCtx; taskId: string }) {
  const load = useLoad(async () => {
    const [task, versions, runners] = await Promise.all([api.getTask(v.api, taskId), api.listTaskVersions(v.api, taskId), api.runners(v.api)]);
    // The current draft if there is one (ADR 0073 — always at least as fresh as the latest saved
    // version, since both kinds of save write it), else the latest saved version, else blank.
    const config = task.hasDraft ? (await api.getTaskDraft(v.api, taskId)).config : task.latestVersion === 0 ? null : (await api.getTaskVersion(v.api, taskId, "latest")).config;
    return { task, versions: versions.items, runners, config };
  }, [v.api, taskId]);

  return (
    <Loaded state={load.state} retry={load.reload}>
      {(d) => <Detail key={`${taskId}:${d.task.latestVersion}:${d.task.draftUpdatedAt ?? ""}`} v={v} {...d} reload={load.reload} />}
    </Loaded>
  );
}

function Detail({
  v,
  task,
  versions,
  runners,
  config: loadedConfig,
  reload,
}: {
  v: ViewCtx;
  task: { id: string; name: string; description: string; createdBy: string };
  versions: { version: number; notes: string; createdBy: string; createdAt: string }[];
  runners: import("../types").RunnerInfo[];
  config: TaskConfig | null;
  reload: () => void;
}) {
  const readOnly = !v.canWrite;
  const [name, setName] = useState(task.name);
  const [description, setDescription] = useState(task.description);
  const [metaBusy, setMetaBusy] = useState(false);
  const [metaError, setMetaError] = useState<{ message: string; field?: string } | null>(null);
  const [metaSaved, setMetaSaved] = useState(false);

  const [config, setConfig] = useState<TaskConfig>(loadedConfig ?? DEFAULT_CONFIG);
  const [baseline] = useState(() => JSON.stringify(loadedConfig ?? DEFAULT_CONFIG));
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<{ message: string; field?: string } | null>(null);
  const [savedAs, setSavedAs] = useState<number | "draft" | null>(null);

  const [deleteError, setDeleteError] = useState<string | null>(null);

  const dirty = JSON.stringify(config) !== baseline;

  async function saveMeta(ev: FormEvent) {
    ev.preventDefault();
    setMetaBusy(true);
    setMetaError(null);
    setMetaSaved(false);
    try {
      await api.updateTask(v.api, task.id, name.trim(), description.trim());
      setMetaSaved(true);
    } catch (err) {
      setMetaError({ message: errorMessage(err), field: err instanceof ApiError ? err.field : undefined });
    } finally {
      setMetaBusy(false);
    }
  }

  async function saveVersion() {
    setSaving(true);
    setSaveError(null);
    try {
      const v2 = await api.saveTaskVersion(v.api, task.id, config, notes.trim());
      setSavedAs(v2.version);
      setNotes("");
      reload();
    } catch (err) {
      setSaveError({ message: errorMessage(err), field: err instanceof ApiError ? err.field : undefined });
    } finally {
      setSaving(false);
    }
  }

  async function saveDraft() {
    setSaving(true);
    setSaveError(null);
    try {
      await api.saveTaskDraft(v.api, task.id, config);
      setSavedAs("draft");
      reload();
    } catch (err) {
      setSaveError({ message: errorMessage(err), field: err instanceof ApiError ? err.field : undefined });
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!window.confirm(`Delete the task "${task.name}"? This cannot be undone.`)) return;
    setDeleteError(null);
    try {
      await api.deleteTask(v.api, task.id);
      v.go({ name: "tasks" });
    } catch (err) {
      setDeleteError(errorMessage(err));
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={task.name}
        subtitle={`Created by ${task.createdBy}`}
        actions={v.canWrite ? <Button variant="danger" onClick={remove}>Delete task</Button> : undefined}
      />
      {deleteError && <Banner tone="error">{deleteError}</Banner>}

      <form onSubmit={saveMeta} aria-label="Task details" className="flex max-w-lg flex-col gap-3">
        <Field id="task-detail-name" label="Name" required error={metaError?.field === "name" ? metaError.message : undefined}>
          {(p) => <input {...p} disabled={readOnly} className={inputClass} value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />}
        </Field>
        <Field id="task-detail-desc" label="Description">
          {(p) => <input {...p} disabled={readOnly} className={inputClass} value={description} maxLength={4000} onChange={(e) => setDescription(e.target.value)} />}
        </Field>
        {metaError && metaError.field !== "name" && <Banner tone="error">{metaError.message}</Banner>}
        {metaSaved && <Banner tone="success">Saved.</Banner>}
        {!readOnly && (
          <div>
            <Button type="submit" disabled={metaBusy || name.trim() === ""}>
              {metaBusy ? "Saving…" : "Save details"}
            </Button>
          </div>
        )}
      </form>

      <section aria-label="Version history" className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">Version history</h3>
        {versions.length === 0 ? (
          <EmptyState title="No versions saved yet">Configure this task below and save its first version.</EmptyState>
        ) : (
          <div className="overflow-x-auto rounded-md border border-slate-200 dark:border-slate-700">
            <table className="w-full">
              <thead className="border-b border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-900">
                <tr>
                  <th className={thClass}>Version</th>
                  <th className={thClass}>Notes</th>
                  <th className={thClass}>By</th>
                  <th className={thClass}>When</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                {versions.map((ver) => (
                  <tr key={ver.version}>
                    <td className={tdClass}>v{ver.version}</td>
                    <td className={tdClass}>{ver.notes || "—"}</td>
                    <td className={tdClass}>{ver.createdBy}</td>
                    <td className={tdClass}>{formatTime(ver.createdAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section aria-label="Current configuration" className="flex max-w-2xl flex-col gap-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">Configuration</h3>
          {dirty && <span className="text-xs font-medium text-amber-600 dark:text-amber-400">Unsaved changes</span>}
        </div>
        <TaskConfigForm
          config={config}
          runners={runners}
          api={v.api}
          readOnly={readOnly}
          error={saveError?.field ? { message: saveError.message, field: saveError.field } : undefined}
          onChange={setConfig}
        />
        {saveError && !saveError.field && <Banner tone="error">{saveError.message}</Banner>}
        {savedAs === "draft" && (
          <Banner tone="success">Saved. "Always follow latest" references pick this up immediately; nothing pinned to a specific version is affected.</Banner>
        )}
        {typeof savedAs === "number" && <Banner tone="success">Saved as version {savedAs}. Pipelines set to follow the latest version will use it from their next save.</Banner>}
        {!readOnly && (
          <>
            <input
              aria-label="Version note"
              className={`${inputClass} max-w-sm`}
              placeholder="What changed? (optional)"
              value={notes}
              maxLength={200}
              onChange={(e) => setNotes(e.target.value)}
            />
            <div className="flex gap-2">
              <Button onClick={saveDraft} disabled={saving || !dirty}>
                {saving ? "Saving…" : "Save"}
              </Button>
              <Button variant="primary" onClick={saveVersion} disabled={saving || !dirty}>
                {saving ? "Saving…" : "Save as new version"}
              </Button>
            </div>
          </>
        )}
      </section>
    </div>
  );
}
