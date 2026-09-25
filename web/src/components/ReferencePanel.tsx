import { useEffect, useState } from "react";
import { ApiError, api, type ApiContext } from "../api/client";
import type { ViewCtx } from "../context";
import { formatTime } from "../format";
import { KEY_RE, checkConnection } from "../graph";
import { errorMessage } from "../hooks";
import type { RunnerInfo, TaskConfig, TaskEntity, TaskRef, TaskVersionSummary } from "../types";
import { TaskConfigForm } from "./TaskConfigForm";
import { TaskPicker } from "./TaskPicker";
import { Banner, Button, Field, Link, Spinner, inputClass, linkClass } from "./ui";

// Configures ONE DAG node (ADR 0071): which task it references, at which version, its
// dependencies and canvas wiring — plus, inline, the referenced task's own config, since editing
// a task from the canvas is expected to feel like the old one-panel-per-node workflow even though
// it now edits a separate, independently versioned entity. Saving the task's config here creates a
// new TASK version immediately (independent of the pipeline's own save); the wiring (key,
// dependsOn, position, which version this node points at) is part of the pipeline draft the parent
// owns, and is only saved when the pipeline itself is saved.

const DEFAULT_CONFIG: TaskConfig = {
  code: { type: "catalog", entryId: "", version: "latest" },
  runner: "base",
  retry: null,
  params: {},
  timeoutSeconds: 3600,
  platformAccess: false,
};

/** Fields this panel has a dedicated slot for, matched against the backend's `field` path relative
 *  to the DAG node ("tasks[N]." already stripped) — a pipeline-save-time error about a reference
 *  that no longer resolves (ADR 0071: `_pin_task_versions`), not about the task's own config. */
const KNOWN_REF_ERROR_FIELDS = new Set(["key", "dependsOn", "taskId", "taskVersion"]);

export interface ReferencePanelProps {
  taskRef: TaskRef;
  refs: TaskRef[];
  runners: RunnerInfo[];
  api: ApiContext;
  v: ViewCtx;
  readOnly: boolean;
  /** A server validation message about this node, and which of its fields (if any) it names. */
  error?: { message: string; field: string | null };
  onChangeRef: (ref: TaskRef) => void;
  onTaskNameKnown: (taskId: string, name: string) => void;
  onRename: (from: string, to: string) => void;
  onSetDependencies: (key: string, dependsOn: string[]) => void;
  onRemove: (key: string) => void;
}

export function ReferencePanel({ taskRef, refs, runners, api: apiCtx, v, readOnly, error, onChangeRef, onTaskNameKnown, onRename, onSetDependencies, onRemove }: ReferencePanelProps) {
  const [keyDraft, setKeyDraft] = useState(taskRef.key);
  const [task, setTask] = useState<TaskEntity | null>(null);
  const [versions, setVersions] = useState<TaskVersionSummary[]>([]);
  const [config, setConfig] = useState<TaskConfig | null>(null);
  const [baseline, setBaseline] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<{ message: string; field: string | null } | null>(null);
  const [saved, setSaved] = useState<"version" | "draft" | null>(null);

  // A different node was selected, or its reference was repointed at a different task/version:
  // (re)load the task entity, its versions, and the config for whichever version this node names.
  // "Always follow latest" loads the task's current DRAFT when it has one (ADR 0073) — always at
  // least as fresh as its latest saved version, since both kinds of save write it — falling back
  // to the latest version, then a blank default for a task that has never been configured at all.
  useEffect(() => {
    setKeyDraft(taskRef.key);
    setSaveError(null);
    setSaved(null);
    let alive = true;
    setTask(null);
    setVersions([]);
    setConfig(null);
    setLoadError(null);
    (async () => {
      try {
        const [t, vs] = await Promise.all([api.getTask(apiCtx, taskRef.taskId), api.listTaskVersions(apiCtx, taskRef.taskId)]);
        if (!alive) return;
        setTask(t);
        setVersions(vs.items);
        onTaskNameKnown(t.id, t.name);
        if (taskRef.taskVersion === "latest" && t.hasDraft) {
          const d = await api.getTaskDraft(apiCtx, taskRef.taskId);
          if (!alive) return;
          setConfig(d.config);
          setBaseline(JSON.stringify(d.config));
          return;
        }
        const resolved = taskRef.taskVersion === "latest" ? t.latestVersion : taskRef.taskVersion;
        if (resolved > 0) {
          const v = await api.getTaskVersion(apiCtx, taskRef.taskId, resolved);
          if (!alive) return;
          setConfig(v.config);
          setBaseline(JSON.stringify(v.config));
        } else {
          setConfig(DEFAULT_CONFIG);
          setBaseline(JSON.stringify(DEFAULT_CONFIG));
        }
      } catch (err) {
        if (alive) setLoadError(errorMessage(err));
      }
    })();
    return () => {
      alive = false;
    };
    // taskRef.key is deliberately excluded: a rename must not re-trigger this load.
  }, [apiCtx, taskRef.taskId, taskRef.taskVersion]);

  const keyError = keyDraft !== taskRef.key && !KEY_RE.test(keyDraft)
    ? "Use lowercase letters, digits and underscores, starting with a letter."
    : keyDraft !== taskRef.key && refs.some((r) => r.key === keyDraft)
      ? "Another task already uses this key."
      : undefined;

  function commitKey() {
    if (keyDraft === taskRef.key) return;
    if (keyError) return setKeyDraft(taskRef.key);
    onRename(taskRef.key, keyDraft);
  }

  async function saveTaskVersion() {
    if (!config) return;
    setSaving(true);
    setSaveError(null);
    setSaved(null);
    try {
      const v = await api.saveTaskVersion(apiCtx, taskRef.taskId, config);
      setBaseline(JSON.stringify(config));
      setVersions((vs) => [{ taskId: v.taskId, version: v.version, notes: v.notes, createdBy: v.createdBy, createdAt: v.createdAt }, ...vs]);
      setTask((t) => (t ? { ...t, latestVersion: v.version, hasDraft: true } : t));
      // The edit just made takes effect on this node immediately, whatever it was pinned to before.
      onChangeRef({ ...taskRef, taskVersion: v.version });
      setSaved("version");
    } catch (err) {
      setSaveError({ message: errorMessage(err), field: err instanceof ApiError ? (err.field ?? null) : null });
    } finally {
      setSaving(false);
    }
  }

  async function saveTaskDraft() {
    if (!config) return;
    setSaving(true);
    setSaveError(null);
    setSaved(null);
    try {
      const d = await api.saveTaskDraft(apiCtx, taskRef.taskId, config);
      setBaseline(JSON.stringify(d.config));
      setTask((t) => (t ? { ...t, hasDraft: true } : t));
      setSaved("draft");
    } catch (err) {
      setSaveError({ message: errorMessage(err), field: err instanceof ApiError ? (err.field ?? null) : null });
    } finally {
      setSaving(false);
    }
  }

  // Only nodes that could legally be upstream of this one are offered (same rules as drawing an edge).
  const candidates = refs.filter((r) => r.key !== taskRef.key && (taskRef.dependsOn.includes(r.key) || checkConnection(refs, r.key, taskRef.key).ok));

  const fieldError = (f: string) => (error?.field === f ? error.message : undefined);
  const showBanner = error && (!error.field || !KNOWN_REF_ERROR_FIELDS.has(error.field));
  const configDirty = config !== null && baseline !== null && JSON.stringify(config) !== baseline;

  return (
    <div role="form" aria-label={`Configure task ${task?.name || taskRef.key}`} className="flex flex-col gap-4">
      {showBanner && <Banner tone="error">{error.message}</Banner>}

      <fieldset disabled={readOnly} className="flex min-w-0 flex-col gap-4 disabled:opacity-90">
        <Field
          id="task-key"
          label="Key"
          required
          error={keyError ?? fieldError("key")}
          help={"Downstream code reads this task's output as ctx.inputs[\"key\"], so renaming it changes what they must read."}
        >
          {(p) => (
            <input
              {...p}
              className={`${inputClass} font-mono`}
              value={keyDraft}
              onChange={(e) => setKeyDraft(e.target.value)}
              onBlur={commitKey}
              onKeyDown={(e) => e.key === "Enter" && commitKey()}
            />
          )}
        </Field>

        <ReferenceTaskField
          apiCtx={apiCtx}
          taskId={taskRef.taskId}
          currentName={task?.name}
          disabled={readOnly}
          error={fieldError("taskId")}
          onPick={(id) => onChangeRef({ ...taskRef, taskId: id, taskVersion: "latest" })}
        />

        {task && (
          <Field id="task-version" label="Version" error={fieldError("taskVersion")} help="Follow the latest to pick up every save on this task, or pin one so this node never changes on its own.">
            {(p) => (
              <select
                {...p}
                className={inputClass}
                value={taskRef.taskVersion}
                onChange={(e) => onChangeRef({ ...taskRef, taskVersion: e.target.value === "latest" ? "latest" : Number(e.target.value) })}
              >
                <option value="latest">Always the latest saved version</option>
                {versions.map((ver) => (
                  <option key={ver.version} value={ver.version}>
                    Pin to v{ver.version} — {formatTime(ver.createdAt)}
                  </option>
                ))}
              </select>
            )}
          </Field>
        )}

        {task && (
          <p className="-mt-2 text-xs text-slate-500 dark:text-slate-400">
            <Link href={v.href({ name: "task", id: task.id })} onNavigate={v.goPath} className={linkClass}>
              Open "{task.name}" in the task library
            </Link>{" "}
            for its description, full version history, or to delete it.
          </p>
        )}

        <section aria-label="Dependencies" className="flex flex-col gap-1.5">
          <h3 className="text-xs font-medium text-slate-600 dark:text-slate-300">Depends on</h3>
          {fieldError("dependsOn") && (
            <p role="alert" className="text-xs text-red-600 dark:text-red-400">
              {fieldError("dependsOn")}
            </p>
          )}
          {candidates.length === 0 ? (
            <p className="text-xs text-slate-500 dark:text-slate-400">No other task can feed this one yet. Add another task, or drag from another task's right-hand handle.</p>
          ) : (
            candidates.map((r) => (
              <label key={r.key} className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
                <input
                  type="checkbox"
                  checked={taskRef.dependsOn.includes(r.key)}
                  onChange={(e) =>
                    onSetDependencies(taskRef.key, e.target.checked ? [...taskRef.dependsOn, r.key] : taskRef.dependsOn.filter((d) => d !== r.key))
                  }
                />
                <span className="font-mono text-xs">{r.key}</span>
              </label>
            ))
          )}
        </section>
      </fieldset>

      {!readOnly && (
        <div>
          <Button variant="danger" onClick={() => onRemove(taskRef.key)}>
            Remove from pipeline
          </Button>
        </div>
      )}

      <hr className="border-slate-200 dark:border-slate-700" />

      {loadError && <Banner tone="error">Could not load this task: {loadError}</Banner>}
      {!loadError && !config && <Spinner label="Loading task…" />}
      {config && (
        <div className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">Task configuration {task && task.latestVersion === 0 && !task.hasDraft ? "(new)" : ""}</h3>
            {configDirty && <span className="text-xs font-medium text-amber-600 dark:text-amber-400">Unsaved</span>}
          </div>
          <TaskConfigForm
            config={config}
            runners={runners}
            api={apiCtx}
            readOnly={readOnly}
            error={saveError ?? undefined}
            onChange={setConfig}
          />
          {saved === "draft" && (
            <Banner tone="success">Saved. "Always follow latest" references pick this up immediately; nothing pinned to a specific version is affected.</Banner>
          )}
          {saved === "version" && <Banner tone="success">Saved as a new task version.</Banner>}
          {!readOnly && (
            <div className="flex gap-2">
              <Button onClick={saveTaskDraft} disabled={saving || !configDirty}>
                {saving ? "Saving…" : "Save"}
              </Button>
              <Button variant="primary" onClick={saveTaskVersion} disabled={saving || !configDirty}>
                {saving ? "Saving…" : "Save as new task version"}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ReferenceTaskField({
  apiCtx,
  taskId,
  currentName,
  disabled,
  error,
  onPick,
}: {
  apiCtx: ApiContext;
  taskId: string;
  currentName?: string;
  disabled?: boolean;
  error?: string;
  onPick: (taskId: string) => void;
}) {
  const [open, setOpen] = useState(false);

  return (
    <Field id="task-picker" label="References task" error={error}>
      {() => (
        <div className="flex flex-col gap-1.5">
          {!open ? (
            <div className="flex items-center gap-2">
              <span className={`${inputClass} flex-1 truncate`}>{currentName || taskId}</span>
              {!disabled && (
                <Button onClick={() => setOpen(true)} aria-label="Change which task this node references">
                  Change
                </Button>
              )}
            </div>
          ) : (
            <TaskPicker
              api={apiCtx}
              onPick={(t) => {
                onPick(t.id);
                setOpen(false);
              }}
              onCancel={() => setOpen(false)}
            />
          )}
        </div>
      )}
    </Field>
  );
}
