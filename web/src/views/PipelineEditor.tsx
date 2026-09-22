import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api/client";
import { DagCanvas } from "../components/DagCanvas";
import { TaskPanel } from "../components/TaskPanel";
import { Banner, Button, Chip, Link, Loaded, PageHeader, inputClass, linkClass } from "../components/ui";
import type { ViewCtx } from "../context";
import { autoLayout, newTask, removeTask, renameKey, taskIndexOfField } from "../graph";
import { errorMessage, useDebounced, useLoad } from "../hooks";
import type { Pipeline, PipelineVersion, RunnerInfo, Task, ValidateResult } from "../types";

// The graphical DAG builder: draw a Pipeline (source -> transform(s) -> sink), configure each
// Task, and save. Every save is a NEW immutable version — the canvas edits a draft, and nothing
// changes for the jobs that run this pipeline until they are pointed at the new version.

export function PipelineEditor({ v, pipelineId, version }: { v: ViewCtx; pipelineId: string; version?: number }) {
  // Lives here, not in Editor: a save reloads the pipeline, the editor is re-keyed to the new
  // latest version and remounts, and its own state (this banner included) would be lost.
  const [savedAs, setSavedAs] = useState<number | null>(null);
  const load = useLoad(
    async () => {
      const [pipeline, versions, runners] = await Promise.all([
        api.getPipeline(v.api, pipelineId),
        api.listVersions(v.api, pipelineId),
        api.runners(v.api),
      ]);
      // A pipeline that has never been saved has no version to load: it starts as a blank canvas.
      const current: PipelineVersion | null =
        pipeline.latestVersion === 0 ? null : await api.getVersion(v.api, pipelineId, version ?? "latest");
      return { pipeline, versions: versions.items, runners, current };
    },
    [v.api, pipelineId, version],
  );
  return (
    <Loaded state={load.state} retry={load.reload}>
      {(d) => <Editor key={`${pipelineId}:${d.current?.version ?? 0}`} v={v} pipeline={d.pipeline} versions={d.versions} runners={d.runners} current={d.current} reload={load.reload} savedAs={savedAs} setSavedAs={setSavedAs} />}
    </Loaded>
  );
}

interface EditorProps {
  v: ViewCtx;
  pipeline: Pipeline;
  versions: { version: number; notes: string; createdBy: string; createdAt: string; taskCount: number }[];
  runners: RunnerInfo[];
  current: PipelineVersion | null;
  reload: () => void;
  savedAs: number | null;
  setSavedAs: (n: number | null) => void;
}

const spec = (tasks: Task[]) => ({ tasks });

function Editor({ v, pipeline, versions, runners, current, reload, savedAs, setSavedAs }: EditorProps) {
  const [tasks, setTasks] = useState<Task[]>(current?.spec.tasks ?? []);
  const [baseline, setBaseline] = useState(() => JSON.stringify(current?.spec.tasks ?? []));
  const [selected, setSelected] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<{ message: string; field?: string } | null>(null);
  const [live, setLive] = useState<ValidateResult | null>(null);

  const dirty = JSON.stringify(tasks) !== baseline;
  const readOnly = !v.canWrite;
  const latest = pipeline.latestVersion;
  const viewingOld = current !== null && current.version !== latest;

  // Warn before a reload/close throws away drawn work.
  useEffect(() => {
    if (!dirty) return;
    const h = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [dirty]);

  // Live validation, debounced, by the SERVER — the single source of truth for the rules, so the
  // canvas can never disagree with what save will accept.
  const debounced = useDebounced(tasks, 500);
  useEffect(() => {
    if (debounced.length === 0) return setLive(null);
    let alive = true;
    api.validate(v.api, spec(debounced)).then(
      (r) => alive && setLive(r),
      () => alive && setLive(null), // the check itself failing is not the user's problem; save reports for real
    );
    return () => {
      alive = false;
    };
  }, [debounced, v.api]);

  const problem = saveError ?? (live && !live.valid ? { message: live.error ?? "invalid", field: live.field ?? undefined } : null);
  const taskErrors = useMemo(() => {
    const idx = taskIndexOfField(problem?.field);
    return idx !== null && tasks[idx] ? { [tasks[idx].key]: problem?.message ?? "" } : {};
  }, [problem, tasks]);

  const change = useCallback((next: Task[]) => {
    setTasks(next);
    setSaveError(null);
    setSavedAs(null);
  }, [setSavedAs]);

  function add() {
    const t = newTask(tasks);
    change([...tasks, t]);
    setSelected(t.key);
  }

  // A suggested starting shape — source -> transform -> sink is one common way to draw a
  // pipeline, not the only legal one (ADR 0062: kind is a label, not a requirement).
  function starter() {
    const s = newTask([], "source");
    const t = { ...newTask([s], "transform"), dependsOn: [s.key] };
    const k = { ...newTask([s, t], "sink"), dependsOn: [t.key] };
    change(autoLayout([s, t, k]));
    setSelected(s.key);
  }

  async function save() {
    setSaving(true);
    setSaveError(null);
    try {
      const saved = await api.saveVersion(v.api, pipeline.id, spec(tasks), notes.trim());
      setSavedAs(saved.version);
      setNotes("");
      // The server's copy carries the resolved code snapshots; adopt it so what is on screen is what was stored.
      setTasks(saved.spec.tasks);
      setBaseline(JSON.stringify(saved.spec.tasks));
      v.goPath(v.href({ name: "pipeline", id: pipeline.id }));
      reload();
    } catch (err) {
      setSaveError({ message: errorMessage(err), field: err instanceof ApiError ? err.field : undefined });
      const idx = err instanceof ApiError ? taskIndexOfField(err.field) : null;
      if (idx !== null && tasks[idx]) setSelected(tasks[idx].key);
    } finally {
      setSaving(false);
    }
  }

  const selectedTask = tasks.find((t) => t.key === selected) ?? null;

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title={pipeline.name}
        subtitle={pipeline.description || undefined}
        actions={
          <>
            {dirty && <Chip tone="amber">Unsaved changes</Chip>}
            {!dirty && latest > 0 && !viewingOld && (
              <Link {...{ href: v.href({ name: "job-new", pipelineId: pipeline.id }), onNavigate: v.goPath }} className={linkClass}>
                Create a job
              </Link>
            )}
          </>
        }
      />

      <div className="flex flex-wrap items-center gap-2" role="toolbar" aria-label="Pipeline builder">
        {!readOnly && (
          <>
            <Button variant="primary" onClick={add}>
              + Add task
            </Button>
            <Button onClick={() => change(autoLayout(tasks))} disabled={tasks.length === 0}>
              Tidy layout
            </Button>
          </>
        )}
        <span className="flex-1" />
        <label htmlFor="version-select" className="text-xs text-slate-500 dark:text-slate-400">
          Version
        </label>
        <select
          id="version-select"
          className={`${inputClass} w-auto`}
          value={current?.version ?? ""}
          disabled={versions.length === 0}
          onChange={(e) => {
            const n = Number(e.target.value);
            v.goPath(v.href(n === latest ? { name: "pipeline", id: pipeline.id } : { name: "pipeline", id: pipeline.id, version: n }));
          }}
        >
          {versions.length === 0 && <option value="">not saved yet</option>}
          {versions.map((ver) => (
            <option key={ver.version} value={ver.version}>
              v{ver.version}
              {ver.version === latest ? " (latest)" : ""} — {ver.taskCount} tasks{ver.notes ? ` — ${ver.notes}` : ""}
            </option>
          ))}
        </select>
        {!readOnly && (
          <>
            <input
              aria-label="Version note"
              className={`${inputClass} w-56`}
              placeholder="What changed? (optional)"
              value={notes}
              maxLength={200}
              onChange={(e) => setNotes(e.target.value)}
            />
            <Button variant="primary" onClick={save} disabled={saving || tasks.length === 0 || (!dirty && latest > 0)}>
              {saving ? "Saving…" : "Save as new version"}
            </Button>
          </>
        )}
      </div>

      {viewingOld && (
        <Banner tone="info">
          You are viewing version {current?.version} of {latest}. Saving creates version {latest + 1} from this one; nothing that runs the latest version changes.
        </Banner>
      )}
      {savedAs !== null && <Banner tone="success">Saved as version {savedAs}. Jobs set to follow the latest version will use it from their next run.</Banner>}
      {saveError && <Banner tone="error">{saveError.message}</Banner>}
      {!saveError && live && !live.valid && <Banner tone="warn">Not ready to save: {live.error}</Banner>}
      {readOnly && <Banner tone="info">Your role in this workspace is read-only, so the builder is view-only.</Banner>}

      <div className="flex min-h-[32rem] flex-col gap-3 lg:flex-row">
        <div className="relative h-[32rem] min-w-0 flex-1 overflow-hidden rounded-md border border-slate-300 bg-slate-50 dark:border-slate-700 dark:bg-slate-950">
          <DagCanvas
            tasks={tasks}
            selected={selected}
            onSelect={setSelected}
            onChange={readOnly ? undefined : change}
            errors={taskErrors}
            theme={v.theme}
          />
          {tasks.length === 0 && (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center p-6">
              <div className="pointer-events-auto max-w-sm rounded-md border border-dashed border-slate-300 bg-white p-6 text-center dark:border-slate-600 dark:bg-slate-900">
                <p className="text-sm font-medium text-slate-800 dark:text-slate-100">This pipeline is empty</p>
                <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">A pipeline flows from a source, through transforms, to a sink.</p>
                {!readOnly && (
                  <div className="mt-3">
                    <Button variant="primary" onClick={starter}>
                      Start with source → transform → sink
                    </Button>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        <aside aria-label="Task settings" className="max-h-[32rem] w-full overflow-auto rounded-md border border-slate-200 p-3 lg:w-[26rem] dark:border-slate-700">
          {selectedTask ? (
            <TaskPanel
              task={selectedTask}
              tasks={tasks}
              runners={runners}
              api={v.api}
              readOnly={readOnly}
              error={taskErrors[selectedTask.key]}
              onChange={(t) => change(tasks.map((x) => (x.key === selectedTask.key ? t : x)))}
              onRename={(from, to) => {
                change(renameKey(tasks, from, to));
                setSelected(to);
              }}
              onSetDependencies={(key, deps) => change(tasks.map((t) => (t.key === key ? { ...t, dependsOn: deps } : t)))}
              onDelete={(key) => {
                change(removeTask(tasks, key));
                setSelected(null);
              }}
            />
          ) : (
            <p className="text-sm text-slate-500 dark:text-slate-400">
              Select a task to configure what code it runs, where it runs, and how it retries.
              {!readOnly && " Drag from a task's right-hand handle to another task to make it depend on this one."}
            </p>
          )}
        </aside>
      </div>
    </div>
  );
}
