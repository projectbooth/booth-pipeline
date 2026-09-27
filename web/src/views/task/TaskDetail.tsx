import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, api, type ApiContext } from "../../api/client";
import type { ViewCtx } from "../../context";
import { codeRefLabel, languageOf } from "../../dag/resolve";
import { formatRelative, formatStamp, formatTime } from "../../format";
import { errorMessage, useLoad } from "../../hooks";
import { TASK_TABS, type TaskTab } from "../../navigation";
import { DEFAULT_CONFIG, TaskSettingsForm } from "../../task/TaskSettingsForm";
import type { TaskConfig, TaskDraft, TaskEntity, TaskVersion, TaskVersionSummary } from "../../types";
import {
  Badge,
  Banner,
  Button,
  Card,
  EmptyState,
  Field,
  InlineConfirm,
  KVRows,
  Link,
  Loaded,
  OverflowMenu,
  PageHeader,
  Tabs,
  inputClass,
  linkClass,
  rowHoverClass,
  tableClass,
  tbodyClass,
  tdClass,
  thClass,
  theadClass,
} from "../../ui/primitives";

// S5 — the task page (docs/decisions/0016 §4, Figma screens 7–9): header with meta chips, then
// Code / Configuration / Versions, and an Edit mode that reuses the builder's task settings form.
// The Figma's task Runs tab and "Run Task" are dropped (ruled Q1: no backend support); Versions
// takes the Runs tab's place, keeping per-entity version history reachable from the entity.

const TABS: { key: TaskTab; label: string }[] = [
  { key: "code", label: "Code" },
  { key: "config", label: "Configuration" },
  { key: "versions", label: "Versions" },
];

interface TaskData {
  task: TaskEntity;
  versions: TaskVersionSummary[]; // newest first
  latest: TaskVersion | null;
  draft: TaskDraft | null;
  /** The explicitly requested version (`?version=N`), when there is one. */
  viewed: TaskVersion | null;
  /** True only when the draft's config actually differs from the latest version's. */
  draftDiffers: boolean;
}

async function loadTask(ctx: ApiContext, id: string, version?: number): Promise<TaskData> {
  const [task, versions] = await Promise.all([api.getTask(ctx, id), api.listTaskVersions(ctx, id)]);
  const [latest, draft] = await Promise.all([
    task.latestVersion > 0 ? api.getTaskVersion(ctx, id, task.latestVersion) : Promise.resolve(null),
    task.hasDraft ? api.getTaskDraft(ctx, id).catch((e: unknown) => (e instanceof ApiError && e.status === 404 ? null : Promise.reject(e))) : Promise.resolve(null),
  ]);
  const viewed = version === undefined ? null : version === latest?.version ? latest : await api.getTaskVersion(ctx, id, version);
  const draftDiffers = draft !== null && JSON.stringify(draft.config) !== JSON.stringify(latest?.config ?? null);
  return { task, versions: versions.items, latest, draft, viewed, draftDiffers };
}

/** What "current" means for a task: its draft (plain Save writes it, and a version-save refreshes
 *  it, so it's never behind), else its latest version. null: never configured. */
const currentConfig = (d: TaskData): TaskConfig | null => d.draft?.config ?? d.latest?.config ?? null;

export function TaskDetail({ v, taskId, tab, version }: { v: ViewCtx; taskId: string; tab: TaskTab; version?: number }) {
  const load = useLoad(() => loadTask(v.api, taskId, version), [v.api, taskId, version]);
  return <Loaded state={load.state} retry={load.reload}>{(d) => <TaskPage v={v} d={d} tab={tab} reload={load.reload} />}</Loaded>;
}

function TaskPage({ v, d, tab, reload }: { v: ViewCtx; d: TaskData; tab: TaskTab; reload: () => void }) {
  const t = d.task;
  const unconfigured = t.latestVersion === 0 && !t.hasDraft;
  // A brand-new task (e.g. straight from "+ New task") opens ready to configure.
  const [editing, setEditing] = useState(unconfigured && v.canWrite);
  const [panel, setPanel] = useState<"none" | "details" | "delete">("none");
  const [deleting, setDeleting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [editDirty, setEditDirty] = useState(false);
  const onDirty = useCallback((dirty: boolean) => setEditDirty(dirty), []);

  // Unsaved task edits → guard navigation away (the task's own tabs stay allowed only while not
  // viewing a specific version, since switching tabs keeps this page mounted).
  useEffect(() => {
    if (!editDirty) return v.setLeaveGuard(null);
    const allowed = new Set(TASK_TABS.map((k) => v.href({ name: "task", id: t.id, tab: k })));
    v.setLeaveGuard({ allows: (path) => allowed.has(path), what: "unsaved changes to this task's settings" });
  }, [editDirty, t.id, v]);
  useEffect(() => () => v.setLeaveGuard(null), [v]);

  const shown: TaskConfig | null = d.viewed?.config ?? currentConfig(d);

  async function remove() {
    setDeleting(true);
    setActionError(null);
    try {
      await api.deleteTask(v.api, t.id);
      v.go({ name: "tasks" });
    } catch (err) {
      setActionError(`Could not delete: ${errorMessage(err)}`);
      setDeleting(false);
      setPanel("none");
    }
  }

  const lang = languageOf(currentConfig(d));
  const cfg = currentConfig(d);

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        crumbs={
          <>
            <Link href={v.href({ name: "tasks" })} onNavigate={v.goPath} className="hover:text-slate-800 dark:hover:text-slate-200">
              ← Tasks
            </Link>
            <span className="mx-1.5">/</span>
            <span className="font-mono text-slate-800 dark:text-slate-200">{t.name}</span>
          </>
        }
        title={<span className="font-mono">{t.name}</span>}
        titleExtra={
          <>
            {lang && (
              <Badge tone="indigo" mono>
                {lang}
              </Badge>
            )}
            <VersionState d={d} />
          </>
        }
        subtitle={t.description || undefined}
        actions={
          v.canWrite ? (
            <>
              {!editing && (
                <Button variant="primary" onClick={() => setEditing(true)}>
                  Edit
                </Button>
              )}
              <OverflowMenu
                label="More task actions"
                items={[
                  { label: "Edit details…", onSelect: () => setPanel("details") },
                  { label: "Delete task…", onSelect: () => setPanel("delete"), danger: true },
                ]}
              />
            </>
          ) : undefined
        }
        below={
          cfg && (
            <ul aria-label="Task summary" className="mt-2 flex flex-wrap gap-2">
              <MetaChip label="Runner" value={cfg.runner} />
              <MetaChip label="Retries" value={cfg.retry ? `${cfg.retry.maxRetries} · ${cfg.retry.backoff}` : "0"} />
              <MetaChip label="Timeout" value={`${cfg.timeoutSeconds}s`} />
              <MetaChip label="Platform access" value={cfg.platformAccess ? "on" : "off"} />
              <MetaChip label="Created by" value={t.createdBy} />
              <MetaChip label="Updated" value={formatStamp(t.draftUpdatedAt && t.draftUpdatedAt > t.updatedAt ? t.draftUpdatedAt : t.updatedAt)} />
            </ul>
          )
        }
      />

      {actionError && <Banner tone="error">{actionError}</Banner>}
      {panel === "details" && (
        <DetailsCard
          v={v}
          t={t}
          onDone={(changed) => {
            setPanel("none");
            if (changed) reload();
          }}
        />
      )}
      {panel === "delete" && (
        <InlineConfirm
          message={
            <>
              Delete task <strong className="font-mono">{t.name}</strong> and all {d.versions.length} of its versions? A task still referenced by a saved pipeline version can't be deleted.
            </>
          }
          confirmLabel="Delete task"
          busyLabel="Deleting…"
          busy={deleting}
          onConfirm={remove}
          onCancel={() => setPanel("none")}
        />
      )}

      {editing ? (
        <Card title={unconfigured ? "Configure this task" : "Edit task settings"}>
          <div className="flex flex-col gap-3 p-4">
            <p className="text-xs text-slate-500 dark:text-slate-400">
              Save updates the draft that every pipeline following this task's "latest" uses immediately. Save as new version makes an immutable, pinnable v{t.latestVersion + 1}. Pinned
              pipelines are unaffected either way.
            </p>
            <TaskSettingsForm
              api={v.api}
              taskId={t.id}
              initial={currentConfig(d) ?? DEFAULT_CONFIG}
              readOnly={false}
              onDirtyChange={onDirty}
              onSaved={() => reload()}
              onCancel={() => setEditing(false)}
            />
          </div>
        </Card>
      ) : unconfigured ? (
        <EmptyState title="This task isn't configured yet">{v.canWrite ? "Use Edit to choose its code and settings." : "Someone with edit access needs to configure it."}</EmptyState>
      ) : (
        <div className="flex flex-col gap-4">
          <Tabs label="Task views" tabs={TABS} active={tab} hrefFor={(k) => v.href({ name: "task", id: t.id, tab: k, version: k === "versions" ? undefined : d.viewed?.version })} onNavigate={v.goPath} />
          {tab !== "versions" && <ShowingPicker v={v} d={d} tab={tab} />}
          {tab === "code" && shown && <CodeTab config={shown} />}
          {tab === "config" && shown && <ConfigTab t={t} config={shown} />}
          {tab === "versions" && <VersionsTab v={v} d={d} />}
        </div>
      )}
    </div>
  );
}

function VersionState({ d }: { d: TaskData }) {
  const t = d.task;
  if (t.latestVersion === 0) return t.hasDraft ? <Badge tone="sky">Draft only</Badge> : <Badge tone="amber">Not configured yet</Badge>;
  return (
    <>
      <Badge tone="slate">v{t.latestVersion}</Badge>
      {d.draftDiffers && (
        <Badge tone="sky" title="Saved with plain Save; not yet a numbered version">
          Draft ahead of v{t.latestVersion}
        </Badge>
      )}
    </>
  );
}

function MetaChip({ label, value }: { label: string; value: string }) {
  return (
    <li className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs dark:border-slate-700 dark:bg-slate-900">
      <span className="text-slate-500 dark:text-slate-400">{label}</span>
      <span className="font-mono text-slate-900 dark:text-slate-100">{value}</span>
    </li>
  );
}

/** "Showing: Current draft ▾" on the Code and Configuration tabs — pick any version by number,
 *  with its creation time (ADR 0074 parity). */
function ShowingPicker({ v, d, tab }: { v: ViewCtx; d: TaskData; tab: TaskTab }) {
  const t = d.task;
  return (
    <label className="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300">
      Showing
      <select
        aria-label="Task version shown"
        className={`${inputClass} w-auto max-w-full`}
        value={d.viewed ? String(d.viewed.version) : ""}
        onChange={(e) => v.go({ name: "task", id: t.id, tab, version: e.target.value === "" ? undefined : Number(e.target.value) })}
      >
        <option value="">{d.draft && (d.draftDiffers || !d.latest) ? "Current draft" : `Current (v${t.latestVersion})`}</option>
        {d.versions.map((ver) => (
          <option key={ver.version} value={ver.version}>
            v{ver.version} — {formatStamp(ver.createdAt)}
            {ver.notes ? ` — ${ver.notes}` : ""}
          </option>
        ))}
      </select>
    </label>
  );
}

/** S5a — the code this task runs: where it comes from, and the snapshot the server saved. */
function CodeTab({ config }: { config: TaskConfig }) {
  const [copied, setCopied] = useState(false);
  const source = config.code.source ?? null;
  const lines = source ? source.replace(/\n$/, "").split("\n") : [];
  const language = config.code.type === "inline" ? "python" : (config.code.language ?? "unknown");

  async function copy() {
    if (!source) return;
    try {
      await navigator.clipboard.writeText(source);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }

  return (
    <section aria-label="Code" className="overflow-hidden rounded-lg border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-4 py-2 dark:border-slate-700">
        <p className="min-w-0 truncate font-mono text-xs text-slate-600 dark:text-slate-300">
          {codeRefLabel(config)} <span className="text-slate-400">|</span> {language}
        </p>
        {source && (
          <Button size="sm" onClick={copy}>
            {copied ? "Copied" : "Copy"}
          </Button>
        )}
      </div>
      {config.code.type === "inline" && (
        <div className="border-b border-slate-200 p-3 dark:border-slate-700">
          <Banner tone="info">This code was written directly in the builder, a retired way of authoring code. It keeps working as saved; Edit it to switch to a catalog or storage source.</Banner>
        </div>
      )}
      {source ? (
        <pre aria-label="Source" className="max-h-[36rem] overflow-auto bg-slate-50 py-3 font-mono text-xs leading-relaxed text-slate-800 dark:bg-slate-950 dark:text-slate-200">
          {lines.map((ln, i) => (
            <div key={i} className="flex">
              <span aria-hidden="true" className="w-12 shrink-0 select-none pr-4 text-right text-slate-400 dark:text-slate-600">
                {i + 1}
              </span>
              <span className="whitespace-pre pr-4">{ln || " "}</span>
            </div>
          ))}
        </pre>
      ) : (
        <p className="px-4 py-6 text-sm text-slate-500 dark:text-slate-400">Source not captured for this version. It is resolved from {config.code.type} when the task runs.</p>
      )}
    </section>
  );
}

/** S5b — flat, read-only configuration (Figma screen 9). */
function ConfigTab({ t, config }: { t: TaskEntity; config: TaskConfig }) {
  const params = Object.entries(config.params);
  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <Card title="Task settings">
        <KVRows
          rows={[
            { label: "Task ID", value: t.id, mono: true },
            { label: "Name", value: t.name, mono: true },
            { label: "Language", value: languageOf(config) ?? "—", mono: true },
            { label: "Runner", value: config.runner, mono: true },
            { label: "Code", value: codeRefLabel(config), mono: true },
            { label: "Retries", value: config.retry ? `${config.retry.maxRetries} (${config.retry.backoff}, ${config.retry.delaySeconds}s delay)` : "none", mono: true },
            { label: "Timeout", value: `${config.timeoutSeconds}s`, mono: true },
            { label: "Platform access", value: config.platformAccess ? "on — gets ctx.storage and ctx.catalog" : "off" },
          ]}
        />
      </Card>
      <Card title="Parameters">
        {params.length === 0 ? (
          <p className="px-4 py-3 text-sm text-slate-500 dark:text-slate-400">No parameters.</p>
        ) : (
          <KVRows rows={params.map(([k, val]) => ({ label: k, value: typeof val === "string" ? val : JSON.stringify(val), mono: true }))} />
        )}
      </Card>
    </div>
  );
}

/** S5c — the task's version history with creation times, plus a Draft row when it differs. */
function VersionsTab({ v, d }: { v: ViewCtx; d: TaskData }) {
  const t = d.task;
  if (d.versions.length === 0 && !d.draft) return <EmptyState title="No versions yet">Save the task as a version to make it pinnable.</EmptyState>;
  return (
    <div className="relative overflow-x-auto">
      <table className={tableClass}>
        <thead className={theadClass}>
          <tr>
            <th className={thClass}>Version</th>
            <th className={thClass}>Created</th>
            <th className={thClass}>By</th>
            <th className={thClass}>Notes</th>
            <th className={thClass}>
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody className={tbodyClass}>
          {d.draft && (d.draftDiffers || !d.latest) && (
            <tr className={rowHoverClass}>
              <td className={tdClass}>
                <Badge tone="sky">Draft</Badge>
              </td>
              <td className={`${tdClass} font-mono text-xs`} title={formatTime(d.draft.updatedAt)}>
                saved {formatRelative(d.draft.updatedAt)}
              </td>
              <td className={tdClass}>{d.draft.updatedBy}</td>
              <td className={`${tdClass} text-slate-500 dark:text-slate-400`}>{d.latest ? `differs from v${d.latest.version}` : "never saved as a version"}</td>
              <td className={`${tdClass} text-right`}>
                <Link href={v.href({ name: "task", id: t.id })} onNavigate={v.goPath} className={linkClass}>
                  View
                </Link>
              </td>
            </tr>
          )}
          {d.versions.map((ver) => (
            <tr key={ver.version} className={rowHoverClass}>
              <td className={tdClass}>
                <span className="font-mono text-sm font-medium">v{ver.version}</span> {ver.version === t.latestVersion && <Badge tone="indigo">latest</Badge>}
              </td>
              <td className={`${tdClass} whitespace-nowrap font-mono text-xs`} title={`${formatTime(ver.createdAt)} (${formatRelative(ver.createdAt)})`}>
                {formatStamp(ver.createdAt)}
              </td>
              <td className={tdClass}>{ver.createdBy}</td>
              <td className={`${tdClass} max-w-sm truncate text-slate-600 dark:text-slate-300`}>{ver.notes || "—"}</td>
              <td className={`${tdClass} text-right`}>
                <Link href={v.href({ name: "task", id: t.id, version: ver.version })} onNavigate={v.goPath} className={linkClass}>
                  View
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DetailsCard({ v, t, onDone }: { v: ViewCtx; t: TaskEntity; onDone: (changed: boolean) => void }) {
  const [name, setName] = useState(t.name);
  const [description, setDescription] = useState(t.description);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; field?: string } | null>(null);

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.updateTask(v.api, t.id, name.trim(), description.trim());
      onDone(true);
    } catch (err) {
      setError({ message: errorMessage(err), field: (err as { field?: string }).field });
      setBusy(false);
    }
  }

  return (
    <Card title="Task details">
      <form onSubmit={submit} aria-label="Task details" className="flex flex-col gap-3 p-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field id="task-name" label="Name" required error={error?.field === "name" ? error.message : undefined}>
            {(p) => <input {...p} className={`${inputClass} font-mono`} value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />}
          </Field>
          <Field id="task-description" label="Description">
            {(p) => <input {...p} className={inputClass} value={description} maxLength={4000} onChange={(e) => setDescription(e.target.value)} />}
          </Field>
        </div>
        {error && error.field !== "name" && <Banner tone="error">{error.message}</Banner>}
        <div className="flex gap-2">
          <Button type="submit" variant="primary" disabled={busy || name.trim() === ""}>
            {busy ? "Saving…" : "Save details"}
          </Button>
          <Button onClick={() => onDone(false)} disabled={busy}>
            Cancel
          </Button>
        </div>
      </form>
    </Card>
  );
}
