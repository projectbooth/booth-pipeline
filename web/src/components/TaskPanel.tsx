import { useEffect, useState } from "react";
import type { ApiContext } from "../api/client";
import { KEY_RE, checkConnection } from "../graph";
import type { Backoff, RunnerInfo, Task, TaskKind } from "../types";
import { CodePicker } from "./CodePicker";
import { StoragePicker } from "./StoragePicker";
import { Banner, Button, Field, inputClass, monoClass } from "./ui";

// Configures ONE task: what code it runs, which runner it targets, its retry override and its
// dependencies — the per-Task settings the brief's definition of done lists. It edits a draft the
// parent owns; nothing here is saved until the parent saves the whole pipeline as a new version.

const KINDS: { value: TaskKind; label: string }[] = [
  { value: "source", label: "Source — where data comes from" },
  { value: "transform", label: "Transform — reshapes data" },
  { value: "sink", label: "Sink — where data ends up" },
];

/** Everything worth naming as its own field in the "Configure task" panel — matched literally
 *  against the backend's `field` path (relative to the task, "tasks[N]." already stripped by
 *  `parseTaskField`), including a discriminated union's own tag where the backend's path has one
 *  (e.g. "code.catalog.entryId", not "code.entryId" — see `parseTaskField`'s own docstring). */
const KNOWN_ERROR_FIELDS = new Set([
  "key",
  "name",
  "kind",
  "runner",
  "timeoutSeconds",
  "params",
  "dependsOn",
  "retry.maxRetries",
  "retry.delaySeconds",
  "retry.backoff",
  "code.catalog.entryId",
  "code.catalog.version",
  "code.storage.backendId",
  "code.storage.path",
]);

export interface TaskPanelProps {
  task: Task;
  tasks: Task[];
  runners: RunnerInfo[];
  api: ApiContext;
  readOnly: boolean;
  /** A server validation message about this task, and which of its fields (if any) it names —
   *  `field: null` (or a field this panel has no dedicated slot for) falls back to the banner. */
  error?: { message: string; field: string | null };
  onChange: (task: Task) => void;
  onRename: (from: string, to: string) => void;
  onSetDependencies: (key: string, dependsOn: string[]) => void;
  onDelete: (key: string) => void;
}

export function TaskPanel({ task, tasks, runners, api, readOnly, error, onChange, onRename, onSetDependencies, onDelete }: TaskPanelProps) {
  const [keyDraft, setKeyDraft] = useState(task.key);
  const [paramsDraft, setParamsDraft] = useState(() => JSON.stringify(task.params, null, 2));
  const [paramsError, setParamsError] = useState<string | null>(null);
  const [showSource, setShowSource] = useState(false);

  // A different task was selected: reset the drafts to it.
  useEffect(() => {
    setKeyDraft(task.key);
    setParamsDraft(JSON.stringify(task.params, null, 2));
    setParamsError(null);
    setShowSource(false);
    // Only re-seed when the SELECTION changes; typing into params must not be reset by its own echo.
  }, [task.key]);

  const keyError = keyDraft !== task.key && !KEY_RE.test(keyDraft)
    ? "Use lowercase letters, digits and underscores, starting with a letter."
    : keyDraft !== task.key && tasks.some((t) => t.key === keyDraft)
      ? "Another task already uses this key."
      : undefined;

  function commitKey() {
    if (keyDraft === task.key) return;
    if (keyError) return setKeyDraft(task.key);
    onRename(task.key, keyDraft);
  }

  function setParams(text: string) {
    setParamsDraft(text);
    try {
      const parsed: unknown = text.trim() === "" ? {} : JSON.parse(text);
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("params must be a JSON object");
      setParamsError(null);
      onChange({ ...task, params: parsed as Record<string, unknown> });
    } catch (e) {
      setParamsError(e instanceof Error ? e.message : "invalid JSON");
    }
  }

  // Only tasks that could legally be upstream of this one are offered (same rules as drawing an edge).
  const candidates = tasks.filter((t) => t.key !== task.key && (task.dependsOn.includes(t.key) || checkConnection(tasks, t.key, task.key).ok));
  const retry = task.retry ?? { maxRetries: 0, delaySeconds: 0, backoff: "fixed" as Backoff };

  // A server error naming one of the fields below is shown right on that field, same pattern as
  // `keyError`; anything else (no field, or a field this panel has no slot for) falls back to the
  // banner, so a message is never silently dropped just because it doesn't map to an input here.
  const fieldError = (f: string) => (error?.field === f ? error.message : undefined);
  const showBanner = error && (!error.field || !KNOWN_ERROR_FIELDS.has(error.field));

  return (
    <form
      aria-label={`Configure task ${task.name || task.key}`}
      className="flex flex-col gap-4"
      onSubmit={(e) => e.preventDefault()}
    >
      {showBanner && <Banner tone="error">{error.message}</Banner>}
      <fieldset disabled={readOnly} className="flex min-w-0 flex-col gap-4 disabled:opacity-90">
        <Field id="task-name" label="Name" error={fieldError("name")} help="Shown on the canvas. Optional.">
          {(p) => <input {...p} className={inputClass} value={task.name} maxLength={200} onChange={(e) => onChange({ ...task, name: e.target.value })} />}
        </Field>

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

        <Field id="task-kind" label="Type" error={fieldError("kind")} help="Purely a label for the canvas — it never changes what a task can connect to.">
          {(p) => (
            <select
              {...p}
              className={inputClass}
              value={task.kind ?? ""}
              onChange={(e) => onChange({ ...task, kind: (e.target.value || null) as TaskKind | null })}
            >
              <option value="">— untagged —</option>
              {KINDS.map((k) => (
                <option key={k.value} value={k.value}>
                  {k.label}
                </option>
              ))}
            </select>
          )}
        </Field>

        <section aria-label="Code" className="flex flex-col gap-2">
          <h3 className="text-xs font-medium text-slate-600 dark:text-slate-300">Code this task runs</h3>
          {task.code.type === "inline" && (
            <Banner tone="info">
              This task's code was written directly in the builder — an older way of authoring code that has been retired. It keeps working as
              saved; pick a catalog or storage source below to replace it.
            </Banner>
          )}
          <div role="radiogroup" aria-label="Where the code comes from" className="flex gap-4 text-sm text-slate-700 dark:text-slate-300">
            <label className="flex items-center gap-1.5">
              <input
                type="radio"
                name="code-type"
                checked={task.code.type === "catalog"}
                onChange={() => onChange({ ...task, code: { type: "catalog", entryId: "", version: "latest" } })}
              />
              From the code catalog
            </label>
            <label className="flex items-center gap-1.5">
              <input
                type="radio"
                name="code-type"
                checked={task.code.type === "storage"}
                onChange={() => onChange({ ...task, code: { type: "storage", backendId: "", path: "" } })}
              />
              From storage
            </label>
          </div>
          {task.code.type === "inline" ? (
            <Field id="task-source" label="Saved source" help="Read-only: new code is picked from the catalog or storage above, not written here.">
              {(p) => <textarea {...p} className={`${inputClass} ${monoClass} h-56 resize-y`} readOnly value={task.code.source ?? ""} />}
            </Field>
          ) : task.code.type === "catalog" ? (
            <CodePicker
              api={api}
              disabled={readOnly}
              value={task.code.entryId ? task.code : null}
              onChange={(code) => onChange({ ...task, code })}
              error={fieldError("code.catalog.entryId") ?? fieldError("code.catalog.version")}
            />
          ) : (
            <StoragePicker
              api={api}
              disabled={readOnly}
              value={task.code.path ? task.code : null}
              onChange={(code) => onChange({ ...task, code })}
              error={fieldError("code.storage.backendId") ?? fieldError("code.storage.path")}
            />
          )}
          {task.code.type !== "inline" && task.code.source && (
            <div>
              <Button onClick={() => setShowSource((v) => !v)} aria-expanded={showSource}>
                {showSource ? "Hide" : "View"} saved source
              </Button>
              {showSource && (
                <pre className={`${monoClass} mt-2 max-h-56 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-2 dark:border-slate-700 dark:bg-slate-950`}>
                  {task.code.source}
                </pre>
              )}
            </div>
          )}
        </section>

        <Field id="task-runner" label="Runs on" error={fieldError("runner")} help="The base runner needs nothing else installed. Spark is only ever a choice you make, and only when it is installed.">
          {(p) => (
            <select {...p} className={inputClass} value={task.runner} onChange={(e) => onChange({ ...task, runner: e.target.value })}>
              {runners.map((r) => (
                <option key={r.id} value={r.id} disabled={!r.available}>
                  {r.displayName}
                  {r.available ? "" : " (unavailable)"}
                </option>
              ))}
              {!runners.some((r) => r.id === task.runner) && <option value={task.runner}>{task.runner} (unavailable)</option>}
            </select>
          )}
        </Field>
        {runners.filter((r) => !r.available && r.reason).map((r) => (
          <p key={r.id} className="-mt-3 text-xs text-slate-500 dark:text-slate-400">
            {r.displayName}: {r.reason}
          </p>
        ))}

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
            candidates.map((t) => (
              <label key={t.key} className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
                <input
                  type="checkbox"
                  checked={task.dependsOn.includes(t.key)}
                  onChange={(e) =>
                    onSetDependencies(task.key, e.target.checked ? [...task.dependsOn, t.key] : task.dependsOn.filter((d) => d !== t.key))
                  }
                />
                <span className="font-mono text-xs">{t.key}</span>
                <span className="text-xs text-slate-500 dark:text-slate-400">{t.name}</span>
              </label>
            ))
          )}
        </section>

        <section aria-label="Retry" className="flex flex-col gap-2">
          <h3 className="text-xs font-medium text-slate-600 dark:text-slate-300">Retry override</h3>
          <p className="-mt-1 text-xs text-slate-500 dark:text-slate-400">Replaces the job's default retry policy for this task. Zero retries means it fails on the first error.</p>
          <div className="grid grid-cols-3 gap-2">
            <Field id="retry-max" label="Max retries" error={fieldError("retry.maxRetries")}>
              {(p) => (
                <input
                  {...p}
                  type="number"
                  min={0}
                  max={10}
                  className={inputClass}
                  value={retry.maxRetries}
                  onChange={(e) => onChange({ ...task, retry: { ...retry, maxRetries: clampInt(e.target.value, 0, 10) } })}
                />
              )}
            </Field>
            <Field id="retry-delay" label="Delay (s)" error={fieldError("retry.delaySeconds")}>
              {(p) => (
                <input
                  {...p}
                  type="number"
                  min={0}
                  className={inputClass}
                  value={retry.delaySeconds}
                  onChange={(e) => onChange({ ...task, retry: { ...retry, delaySeconds: Math.max(0, Number(e.target.value) || 0) } })}
                />
              )}
            </Field>
            <Field id="retry-backoff" label="Backoff" error={fieldError("retry.backoff")}>
              {(p) => (
                <select {...p} className={inputClass} value={retry.backoff} onChange={(e) => onChange({ ...task, retry: { ...retry, backoff: e.target.value as Backoff } })}>
                  <option value="fixed">Fixed</option>
                  <option value="linear">Linear</option>
                  <option value="exponential">Exponential</option>
                </select>
              )}
            </Field>
          </div>
          {task.retry && (
            <div>
              <Button onClick={() => onChange({ ...task, retry: null })}>Use the job's default</Button>
            </div>
          )}
        </section>

        <section aria-label="Platform access" className="flex flex-col gap-1.5">
          <label className="flex items-start gap-2 text-sm text-slate-700 dark:text-slate-300">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={task.platformAccess}
              onChange={(e) => onChange({ ...task, platformAccess: e.target.checked })}
            />
            <span>
              <span className="font-medium">Platform access</span> — let this task read and write booth-storage and the catalog
            </span>
          </label>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            Gives the task <code>ctx.storage</code> and <code>ctx.catalog</code>, acting as this job's run with at most the job's role ceiling (and never more
            than the run's owner currently holds). Off by default: a task that doesn't need them never asks for an identity, so it can't be blocked by
            one. If the owner hasn't signed in to the platform recently, a task that needs access fails with that reason.
          </p>
        </section>

        <Field id="task-timeout" label="Timeout (seconds)" error={fieldError("timeoutSeconds")} help="The task is killed if it runs longer than this.">
          {(p) => (
            <input
              {...p}
              type="number"
              min={1}
              className={inputClass}
              value={task.timeoutSeconds}
              onChange={(e) => onChange({ ...task, timeoutSeconds: clampInt(e.target.value, 1, 86400) })}
            />
          )}
        </Field>

        <Field id="task-params" label="Parameters (JSON)" error={paramsError ?? fieldError("params")} help="Available to the task as ctx.params.">
          {(p) => <textarea {...p} className={`${inputClass} ${monoClass} h-24 resize-y`} spellCheck={false} value={paramsDraft} onChange={(e) => setParams(e.target.value)} />}
        </Field>
      </fieldset>

      {!readOnly && (
        <div>
          <Button variant="danger" onClick={() => onDelete(task.key)}>
            Delete task
          </Button>
        </div>
      )}
    </form>
  );
}

function clampInt(raw: string, lo: number, hi: number): number {
  const n = Math.trunc(Number(raw));
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : lo;
}
