import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { DagCanvas } from "../components/DagCanvas";
import { LogViewer } from "../components/LogViewer";
import { Banner, Button, Link, Loaded, PageHeader, StatusChip, linkClass, tdClass, thClass } from "../components/ui";
import type { ViewCtx } from "../context";
import { formatDuration, formatTime } from "../format";
import { errorMessage, useInterval, useLoad, type LoadState } from "../hooks";
import type { PipelineVersion, RunDetail, TaskEntity, TaskStatus } from "../types";

// One Run: the DAG coloured by each Task's live status, a per-task table, and logs for the whole
// run or a single Task. While the run is active everything refreshes; once it ends it settles.

const ACTIVE = ["queued", "running"];

export function RunView({ v, runId }: { v: ViewCtx; runId: string }) {
  const load = useLoad(() => api.getRun(v.api, runId), [v.api, runId]);
  // The run's pipeline version supplies the graph the statuses are drawn on. Immutable, so fetched
  // once, along with each referenced task's name (bulk, not per-node — the run view is read-only
  // and never needs a task's own config, only what to label its node).
  const spec = useLoad(async () => {
    const run = await api.getRun(v.api, runId);
    const version = await api.getVersion(v.api, run.pipelineId, run.pipelineVersion);
    const tasks = await Promise.all(version.spec.tasks.map((t) => api.getTask(v.api, t.taskId).catch((): TaskEntity | null => null)));
    const taskNames = Object.fromEntries(tasks.filter((t): t is TaskEntity => t !== null).map((t) => [t.id, t.name]));
    return { version, taskNames };
  }, [v.api, runId]);
  const [selected, setSelected] = useState<string | null>(null);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());

  const run = load.state.status === "ready" ? load.state.data : null;
  const active = run !== null && ACTIVE.includes(run.status);

  // Refresh while active; the interval stops by itself once the run is terminal.
  useInterval(load.reload, 1500, active);
  useInterval(() => setNow(Date.now()), 1000, active);
  useEffect(() => setSelected(null), [runId]);

  async function cancel() {
    setCancelError(null);
    try {
      await api.cancelRun(v.api, runId);
      load.reload();
    } catch (err) {
      setCancelError(errorMessage(err));
    }
  }

  return (
    <Loaded state={load.state} retry={load.reload}>
      {(r) => (
        <div className="flex flex-col gap-4">
          <PageHeader
            title={`Run ${r.id.slice(0, 8)}`}
            subtitle={`Pipeline version ${r.pipelineVersion} · ${r.trigger === "schedule" ? "scheduled" : `started by ${r.triggeredBy}`} · ${formatTime(r.startedAt ?? r.createdAt)}`}
            actions={
              <>
                <StatusChip status={r.status} />
                {v.canWrite && ACTIVE.includes(r.status) && !r.cancelRequested && <Button onClick={cancel}>Cancel run</Button>}
                {r.cancelRequested && ACTIVE.includes(r.status) && <span className="text-xs text-slate-500">Cancelling…</span>}
                <Link href={v.href({ name: "pipeline", id: r.pipelineId })} onNavigate={v.goPath} className={linkClass}>
                  Back to pipeline
                </Link>
              </>
            }
          />
          {cancelError && <Banner tone="error">{cancelError}</Banner>}
          {r.error && <Banner tone={r.status === "canceled" ? "warn" : "error"}>{r.error}</Banner>}

          <Graph v={v} run={r} spec={spec} selected={selected} setSelected={setSelected} />

          <section aria-label="Tasks" className="overflow-x-auto rounded-md border border-slate-200 dark:border-slate-700">
            <table className="w-full">
              <thead className="border-b border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-900">
                <tr>
                  <th className={thClass}>Task</th>
                  <th className={thClass}>Status</th>
                  <th className={thClass}>Attempts</th>
                  <th className={thClass}>Duration</th>
                  <th className={thClass}>Error</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                {r.tasks.map((t) => (
                  <tr key={t.taskKey} className={selected === t.taskKey ? "bg-indigo-50 dark:bg-indigo-950/40" : undefined}>
                    <td className={tdClass}>
                      <button
                        type="button"
                        className={`${linkClass} font-mono text-xs`}
                        aria-pressed={selected === t.taskKey}
                        onClick={() => setSelected(selected === t.taskKey ? null : t.taskKey)}
                      >
                        {t.taskKey}
                      </button>
                    </td>
                    <td className={tdClass}>
                      <StatusChip status={t.status} />
                    </td>
                    <td className={tdClass}>{t.attempts}</td>
                    <td className={tdClass}>{formatDuration(t.startedAt, t.finishedAt, now)}</td>
                    <td className={`${tdClass} max-w-xs truncate text-red-700 dark:text-red-400`} title={t.error ?? undefined}>
                      {t.error ?? ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section aria-label="Logs" className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">{selected ? `Logs — ${selected}` : "Logs — whole run"}</h3>
              {selected && <Button onClick={() => setSelected(null)}>Show the whole run</Button>}
            </div>
            <LogViewer api={v.api} runId={r.id} taskKey={selected} runActive={ACTIVE.includes(r.status)} />
          </section>
        </div>
      )}
    </Loaded>
  );
}

function Graph({
  v,
  run,
  spec,
  selected,
  setSelected,
}: {
  v: ViewCtx;
  run: RunDetail;
  spec: { state: LoadState<{ version: PipelineVersion; taskNames: Record<string, string> }> };
  selected: string | null;
  setSelected: (k: string | null) => void;
}) {
  const statuses = useMemo(() => Object.fromEntries(run.tasks.map((t) => [t.taskKey, t.status])) as Record<string, TaskStatus>, [run.tasks]);
  if (spec.state.status === "loading") return null;
  if (spec.state.status === "error") return <Banner tone="warn">The pipeline graph could not be loaded ({spec.state.error}); the task table below is still accurate.</Banner>;
  return (
    <div className="h-80 overflow-hidden rounded-md border border-slate-300 bg-slate-50 dark:border-slate-700 dark:bg-slate-950">
      <DagCanvas
        refs={spec.state.data.version.spec.tasks}
        taskNames={spec.state.data.taskNames}
        selected={selected}
        onSelect={setSelected}
        statuses={statuses}
        theme={v.theme}
      />
    </div>
  );
}
