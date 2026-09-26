import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { ViewCtx } from "../context";
import { formatStamp } from "../format";
import { KEY_RE, checkConnection } from "../graph";
import type { RunDetail, TaskRef, TaskVersionSummary } from "../types";
import { Banner, Button, Card, Field, Link, inputClass, linkClass } from "../ui/primitives";
import { DEFAULT_CONFIG, TaskSettingsForm } from "../task/TaskSettingsForm";
import { TaskPicker } from "../task/TaskPicker";
import { LastRunStrip } from "./NodeDetails";
import { resolvedFromLabel, type ResolvedNode } from "./resolve";

// Panel (c) of the builder (docs/decisions/0016 §4 S2a): one selected node, rendered BELOW the
// canvas. Its wiring (key, task, version, dependencies) is part of the pipeline edit the page owns
// and is saved with the pipeline. The referenced task's own settings are a separate, shared entity
// with their own saves — the panel says so, because an edit there affects every pipeline that
// follows that task's "latest".

/** Fields with their own slot here, matched against the server's field path relative to the node
 *  ("tasks[N]." stripped). Anything else is shown as a banner so it's never silently dropped. */
const REF_FIELDS = new Set(["key", "dependsOn", "taskId", "taskVersion"]);

export function NodeEditor({
  v,
  taskRef,
  refs,
  node,
  lastRun,
  runCounts,
  error,
  onChangeRef,
  onRename,
  onSetDeps,
  onRemove,
  onTaskSaved,
  onTaskDirty,
}: {
  v: ViewCtx;
  taskRef: TaskRef;
  refs: TaskRef[];
  node: ResolvedNode | undefined;
  lastRun: RunDetail | null;
  runCounts: boolean;
  error?: { message: string; field: string | null };
  onChangeRef: (next: TaskRef) => void;
  onRename: (from: string, to: string) => void;
  onSetDeps: (key: string, deps: string[]) => void;
  onRemove: (key: string) => void;
  onTaskSaved: () => void;
  onTaskDirty: (dirty: boolean) => void;
}) {
  const [keyDraft, setKeyDraft] = useState(taskRef.key);
  const [changing, setChanging] = useState(false);
  const [versions, setVersions] = useState<TaskVersionSummary[] | null>(null);
  const [savedCount, setSavedCount] = useState(0);

  useEffect(() => setKeyDraft(taskRef.key), [taskRef.key]);
  useEffect(() => setChanging(false), [taskRef.key]);

  // The task's versions, for the pin picker (with creation times — ADR 0074 parity).
  useEffect(() => {
    let alive = true;
    setVersions(null);
    api.listTaskVersions(v.api, taskRef.taskId).then(
      (page) => alive && setVersions(page.items),
      () => alive && setVersions([]),
    );
    return () => {
      alive = false;
    };
  }, [v.api, taskRef.taskId, savedCount]);

  const keyError =
    keyDraft !== taskRef.key && !KEY_RE.test(keyDraft)
      ? "Use lowercase letters, digits and underscores, starting with a letter."
      : keyDraft !== taskRef.key && refs.some((r) => r.key === keyDraft)
        ? "Another node already uses this key."
        : undefined;

  function commitKey() {
    if (keyDraft === taskRef.key) return;
    if (keyError) return setKeyDraft(taskRef.key);
    onRename(taskRef.key, keyDraft);
  }

  const fieldError = (f: string) => (error?.field === f ? error.message : undefined);
  const bannerError = error && (!error.field || !REF_FIELDS.has(error.field)) ? error.message : null;
  // Only nodes that could legally feed this one (same rules as drawing an edge: no cycles).
  const candidates = refs.filter((r) => r.key !== taskRef.key && (taskRef.dependsOn.includes(r.key) || checkConnection(refs, r.key, taskRef.key).ok));
  const task = node?.task ?? null;
  const pinned = taskRef.taskVersion !== "latest";

  return (
    <div className="flex flex-col gap-4">
      <Card
        label={`Node ${taskRef.key}`}
        title={
          <span>
            Node <span className="font-mono">{taskRef.key}</span>
          </span>
        }
        actions={
          <Button size="sm" variant="danger" onClick={() => onRemove(taskRef.key)}>
            Remove from pipeline
          </Button>
        }
      >
        <LastRunStrip v={v} nodeKey={taskRef.key} lastRun={runCounts ? lastRun : null} />
        <div className="flex flex-col gap-4 p-4">
          {bannerError && <Banner tone="error">{bannerError}</Banner>}
          <div className="grid gap-4 md:grid-cols-2">
            <Field id="node-key" label="Key" required error={keyError ?? fieldError("key")} help={'Downstream code reads this node\'s output as ctx.inputs["key"], so renaming it changes what they read.'}>
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

            <Field id="node-task" label="References task" error={fieldError("taskId")}>
              {(p) => (
                <div className="flex items-center gap-2">
                  <span {...p} className={`${inputClass} flex-1 truncate font-mono`}>
                    {task?.name ?? node?.error ?? "…"}
                  </span>
                  {!changing && (
                    <Button onClick={() => setChanging(true)} aria-label="Change which task this node references">
                      Change
                    </Button>
                  )}
                  {task && (
                    <Link href={v.href({ name: "task", id: task.id })} onNavigate={v.goPath} className={`${linkClass} whitespace-nowrap text-sm`}>
                      Open task →
                    </Link>
                  )}
                </div>
              )}
            </Field>
          </div>

          {changing && (
            <TaskPicker
              api={v.api}
              title="Point this node at a different task"
              onPick={(t) => {
                setChanging(false);
                onChangeRef({ ...taskRef, taskId: t.id, taskVersion: "latest" });
              }}
              onCancel={() => setChanging(false)}
            />
          )}

          <div className="grid gap-4 md:grid-cols-2">
            <Field
              id="node-version"
              label="Version"
              error={fieldError("taskVersion")}
              help={pinned ? "Pinned: this node never changes on its own." : `Follows the task's latest save${node ? ` (currently ${resolvedFromLabel(node.from)})` : ""}.`}
            >
              {(p) => (
                <select
                  {...p}
                  className={inputClass}
                  value={String(taskRef.taskVersion)}
                  onChange={(e) => onChangeRef({ ...taskRef, taskVersion: e.target.value === "latest" ? "latest" : Number(e.target.value) })}
                >
                  <option value="latest">Always follow latest (draft if saved)</option>
                  {(versions ?? []).map((ver) => (
                    <option key={ver.version} value={ver.version}>
                      Pin to v{ver.version} — {formatStamp(ver.createdAt)}
                      {ver.notes ? ` — ${ver.notes}` : ""}
                    </option>
                  ))}
                  {pinned && versions && !versions.some((ver) => ver.version === taskRef.taskVersion) && (
                    <option value={taskRef.taskVersion}>Pin to v{taskRef.taskVersion}</option>
                  )}
                </select>
              )}
            </Field>

            <fieldset className="flex min-w-0 flex-col gap-1">
              <legend className="mb-1 text-xs font-medium text-slate-600 dark:text-slate-300">Depends on</legend>
              {fieldError("dependsOn") && (
                <p role="alert" className="text-xs text-red-600 dark:text-red-400">
                  {fieldError("dependsOn")}
                </p>
              )}
              {candidates.length === 0 ? (
                <p className="text-xs text-slate-500 dark:text-slate-400">No other node can feed this one without creating a cycle.</p>
              ) : (
                <div className="flex flex-wrap gap-x-4 gap-y-1">
                  {candidates.map((r) => (
                    <label key={r.key} className="flex items-center gap-1.5 text-sm text-slate-700 dark:text-slate-300">
                      <input
                        type="checkbox"
                        checked={taskRef.dependsOn.includes(r.key)}
                        onChange={(e) => onSetDeps(taskRef.key, e.target.checked ? [...taskRef.dependsOn, r.key] : taskRef.dependsOn.filter((x) => x !== r.key))}
                      />
                      <span className="font-mono text-xs">{r.key}</span>
                    </label>
                  ))}
                </div>
              )}
            </fieldset>
          </div>
        </div>
      </Card>

      <Card
        label="Task settings"
        title={
          <span>
            Task settings{task && <span className="font-normal text-slate-500 dark:text-slate-400"> — {task.name}</span>}
          </span>
        }
      >
        <div className="flex flex-col gap-3 p-4">
          {pinned ? (
            <Banner tone="info">
              Pinned to v{taskRef.taskVersion}: pinned versions are frozen, so these settings are read-only here. Switch the version above to "Always follow latest", or open the task, to edit it.
            </Banner>
          ) : (
            <p className="text-xs text-slate-500 dark:text-slate-400">
              These are the shared task's own settings. Saving them affects every pipeline whose node follows this task's latest — not only this one.
            </p>
          )}
          {node?.error && !node.task ? (
            <Banner tone="error">Could not load this task: {node.error}</Banner>
          ) : !node || !resolvesTo(node, taskRef) ? (
            // Right after a re-point or re-pin, the resolved data still describes the old target.
            <p className="text-sm text-slate-500 dark:text-slate-400">Loading task…</p>
          ) : (
            <TaskSettingsForm
              // A different task or version means a fresh form. A save does NOT: the form already
              // holds what it saved, and keeps showing its own confirmation.
              key={`${taskRef.taskId}:${taskRef.taskVersion}`}
              api={v.api}
              taskId={taskRef.taskId}
              initial={node.config ?? DEFAULT_CONFIG}
              readOnly={pinned}
              onDirtyChange={onTaskDirty}
              onSaved={() => {
                setSavedCount((n) => n + 1);
                onTaskSaved();
              }}
            />
          )}
        </div>
      </Card>
    </div>
  );
}

/** Whether `node` (resolved asynchronously) describes what `ref` points at right now. */
function resolvesTo(node: ResolvedNode, ref: TaskRef): boolean {
  return node.forTask === ref.taskId && node.forVersion === ref.taskVersion;
}
