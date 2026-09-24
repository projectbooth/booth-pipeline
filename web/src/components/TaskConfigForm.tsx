import { useState } from "react";
import type { ApiContext } from "../api/client";
import type { Backoff, RunnerInfo, TaskConfig } from "../types";
import { CodePicker } from "./CodePicker";
import { StoragePicker } from "./StoragePicker";
import { Banner, Button, Field, inputClass, monoClass } from "./ui";

// Configures ONE task version's own settings (ADR 0071): what code it runs, which runner it
// targets, its retry policy, platform access and params. Used both standalone (the Task library's
// detail page) and embedded in the canvas's reference panel (ReferencePanel) when a node is
// selected — either way it edits a draft the caller owns; nothing here is saved by itself.

/** Everything worth naming as its own field, matched literally against the backend's `field` path
 *  (relative to the task's own config — see model.py's `validate_task_config`/pydantic errors),
 *  including a discriminated union's own tag where the backend's path has one (e.g.
 *  "code.catalog.entryId", not "code.entryId"). */
export const KNOWN_CONFIG_ERROR_FIELDS = new Set([
  "runner",
  "timeoutSeconds",
  "params",
  "retry.maxRetries",
  "retry.delaySeconds",
  "retry.backoff",
  "code.catalog.entryId",
  "code.catalog.version",
  "code.storage.backendId",
  "code.storage.path",
]);

export interface TaskConfigFormProps {
  config: TaskConfig;
  runners: RunnerInfo[];
  api: ApiContext;
  readOnly: boolean;
  /** A server validation message about this config, and which of its fields (if any) it names —
   *  `field: null` (or a field this form has no dedicated slot for) falls back to the banner. */
  error?: { message: string; field: string | null };
  onChange: (config: TaskConfig) => void;
}

export function TaskConfigForm({ config, runners, api, readOnly, error, onChange }: TaskConfigFormProps) {
  const [paramsDraft, setParamsDraft] = useState(() => JSON.stringify(config.params, null, 2));
  const [paramsError, setParamsError] = useState<string | null>(null);
  const [showSource, setShowSource] = useState(false);

  function setParams(text: string) {
    setParamsDraft(text);
    try {
      const parsed: unknown = text.trim() === "" ? {} : JSON.parse(text);
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("params must be a JSON object");
      setParamsError(null);
      onChange({ ...config, params: parsed as Record<string, unknown> });
    } catch (e) {
      setParamsError(e instanceof Error ? e.message : "invalid JSON");
    }
  }

  const retry = config.retry ?? { maxRetries: 0, delaySeconds: 0, backoff: "fixed" as Backoff };

  // A server error naming one of the fields below is shown right on that field; anything else (no
  // field, or a field this form has no slot for) falls back to the banner, so a message is never
  // silently dropped just because it doesn't map to an input here.
  const fieldError = (f: string) => (error?.field === f ? error.message : undefined);
  const showBanner = error && (!error.field || !KNOWN_CONFIG_ERROR_FIELDS.has(error.field));

  return (
    <div className="flex flex-col gap-4">
      {showBanner && <Banner tone="error">{error.message}</Banner>}
      <fieldset disabled={readOnly} className="flex min-w-0 flex-col gap-4 disabled:opacity-90">
        <section aria-label="Code" className="flex flex-col gap-2">
          <h3 className="text-xs font-medium text-slate-600 dark:text-slate-300">Code this task runs</h3>
          {config.code.type === "inline" && (
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
                checked={config.code.type === "catalog"}
                onChange={() => onChange({ ...config, code: { type: "catalog", entryId: "", version: "latest" } })}
              />
              From the code catalog
            </label>
            <label className="flex items-center gap-1.5">
              <input
                type="radio"
                name="code-type"
                checked={config.code.type === "storage"}
                onChange={() => onChange({ ...config, code: { type: "storage", backendId: "", path: "" } })}
              />
              From storage
            </label>
          </div>
          {config.code.type === "inline" ? (
            <Field id="task-source" label="Saved source" help="Read-only: new code is picked from the catalog or storage above, not written here.">
              {(p) => <textarea {...p} className={`${inputClass} ${monoClass} h-56 resize-y`} readOnly value={config.code.source ?? ""} />}
            </Field>
          ) : config.code.type === "catalog" ? (
            <CodePicker
              api={api}
              disabled={readOnly}
              value={config.code.entryId ? config.code : null}
              onChange={(code) => onChange({ ...config, code })}
              error={fieldError("code.catalog.entryId") ?? fieldError("code.catalog.version")}
            />
          ) : (
            <StoragePicker
              api={api}
              disabled={readOnly}
              value={config.code.path ? config.code : null}
              onChange={(code) => onChange({ ...config, code })}
              error={fieldError("code.storage.backendId") ?? fieldError("code.storage.path")}
            />
          )}
          {config.code.type !== "inline" && config.code.source && (
            <div>
              <Button onClick={() => setShowSource((v) => !v)} aria-expanded={showSource}>
                {showSource ? "Hide" : "View"} saved source
              </Button>
              {showSource && (
                <pre className={`${monoClass} mt-2 max-h-56 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-2 dark:border-slate-700 dark:bg-slate-950`}>
                  {config.code.source}
                </pre>
              )}
            </div>
          )}
        </section>

        <Field id="task-runner" label="Runs on" error={fieldError("runner")} help="The base runner needs nothing else installed. Spark is only ever a choice you make, and only when it is installed.">
          {(p) => (
            <select {...p} className={inputClass} value={config.runner} onChange={(e) => onChange({ ...config, runner: e.target.value })}>
              {runners.map((r) => (
                <option key={r.id} value={r.id} disabled={!r.available}>
                  {r.displayName}
                  {r.available ? "" : " (unavailable)"}
                </option>
              ))}
              {!runners.some((r) => r.id === config.runner) && <option value={config.runner}>{config.runner} (unavailable)</option>}
            </select>
          )}
        </Field>
        {runners.filter((r) => !r.available && r.reason).map((r) => (
          <p key={r.id} className="-mt-3 text-xs text-slate-500 dark:text-slate-400">
            {r.displayName}: {r.reason}
          </p>
        ))}

        <section aria-label="Retry" className="flex flex-col gap-2">
          <h3 className="text-xs font-medium text-slate-600 dark:text-slate-300">Retry policy</h3>
          <p className="-mt-1 text-xs text-slate-500 dark:text-slate-400">Zero retries means the task fails on the first error.</p>
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
                  onChange={(e) => onChange({ ...config, retry: { ...retry, maxRetries: clampInt(e.target.value, 0, 10) } })}
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
                  onChange={(e) => onChange({ ...config, retry: { ...retry, delaySeconds: Math.max(0, Number(e.target.value) || 0) } })}
                />
              )}
            </Field>
            <Field id="retry-backoff" label="Backoff" error={fieldError("retry.backoff")}>
              {(p) => (
                <select {...p} className={inputClass} value={retry.backoff} onChange={(e) => onChange({ ...config, retry: { ...retry, backoff: e.target.value as Backoff } })}>
                  <option value="fixed">Fixed</option>
                  <option value="linear">Linear</option>
                  <option value="exponential">Exponential</option>
                </select>
              )}
            </Field>
          </div>
        </section>

        <section aria-label="Platform access" className="flex flex-col gap-1.5">
          <label className="flex items-start gap-2 text-sm text-slate-700 dark:text-slate-300">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={config.platformAccess}
              onChange={(e) => onChange({ ...config, platformAccess: e.target.checked })}
            />
            <span>
              <span className="font-medium">Platform access</span> — let this task read and write booth-storage and the catalog
            </span>
          </label>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            Gives the task <code>ctx.storage</code> and <code>ctx.catalog</code>, acting as the run with at most the pipeline's role ceiling (and
            never more than the run's owner currently holds). Off by default: a task that doesn't need them never asks for an identity, so it
            can't be blocked by one. If the owner hasn't signed in to the platform recently, a task that needs access fails with that reason.
          </p>
        </section>

        <Field id="task-timeout" label="Timeout (seconds)" error={fieldError("timeoutSeconds")} help="The task is killed if it runs longer than this.">
          {(p) => (
            <input
              {...p}
              type="number"
              min={1}
              className={inputClass}
              value={config.timeoutSeconds}
              onChange={(e) => onChange({ ...config, timeoutSeconds: clampInt(e.target.value, 1, 86400) })}
            />
          )}
        </Field>

        <Field id="task-params" label="Parameters (JSON)" error={paramsError ?? fieldError("params")} help="Available to the task as ctx.params.">
          {(p) => <textarea {...p} className={`${inputClass} ${monoClass} h-24 resize-y`} spellCheck={false} value={paramsDraft} onChange={(e) => setParams(e.target.value)} />}
        </Field>
      </fieldset>
    </div>
  );
}

function clampInt(raw: string, lo: number, hi: number): number {
  const n = Math.trunc(Number(raw));
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : lo;
}
