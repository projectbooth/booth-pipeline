import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { ViewCtx } from "../context";
import { DagCanvas } from "../dag/DagCanvas";
import { useResolved } from "../dag/resolve";
import { formatDuration, formatRelative, formatStamp, formatTime } from "../format";
import { autoLayout, hasOverlappingPositions } from "../graph";
import { errorMessage, useInterval, useLoad } from "../hooks";
import { isActive, runStart } from "../runs";
import type { Pipeline, PipelineVersion, RunDetail as Run, TaskRef, TaskStatus } from "../types";
import {
  Banner,
  Button,
  Link,
  Loaded,
  StatStrip,
  StatusBadge,
  linkClass,
  rowHoverClass,
  tableClass,
  tbodyClass,
  tdClass,
  thClass,
  theadClass,
} from "../ui/primitives";
import { LogViewer } from "./run/LogViewer";

// S3 — one Run (docs/decisions/0016 §4): its DAG coloured by each node's status, a per-node
// table, and logs for the whole run or one node. Refreshes while active, settles once it ends.

const RUN_CANVAS_HEIGHT = 420;

export function RunDetail({ v, runId, initialTask }: { v: ViewCtx; runId: string; initialTask?: string }) {
  const load = useLoad(() => api.getRun(v.api, runId), [v.api, runId]);
  const run = load.state.status === "ready" ? load.state.data : null;
  useInterval(load.reload, 1500, isActive(run));
  return (
    <Loaded state={load.state} retry={load.reload}>
      {(r) => <RunPage v={v} run={r} reload={load.reload} initialTask={initialTask} />}
    </Loaded>
  );
}

function RunPage({ v, run, reload, initialTask }: { v: ViewCtx; run: Run; reload: () => void; initialTask?: string }) {
  const [selected, setSelected] = useState<string | null>(initialTask ?? null);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  const active = isActive(run);
  useInterval(() => setNow(Date.now()), 1000, active);

  // The run's pipeline version supplies the graph (immutable, so fetched once); the pipeline, for
  // its name in the breadcrumb. Neither failing stops the page — the table and logs still work.
  const [pipeline, setPipeline] = useState<Pipeline | null>(null);
  const [version, setVersion] = useState<PipelineVersion | null>(null);
  const [graphError, setGraphError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    api.getPipeline(v.api, run.pipelineId).then((p) => alive && setPipeline(p), () => undefined);
    api.getVersion(v.api, run.pipelineId, run.pipelineVersion).then(
      (ver) => alive && setVersion(ver),
      (err: unknown) => alive && setGraphError(errorMessage(err)),
    );
    return () => {
      alive = false;
    };
  }, [v.api, run.pipelineId, run.pipelineVersion]);

  const refs: TaskRef[] = useMemo(() => {
    const t = version?.spec.tasks ?? [];
    return hasOverlappingPositions(t) ? autoLayout(t) : t;
  }, [version]);
  const { resolved } = useResolved(v.api, refs);
  const statuses = useMemo(() => Object.fromEntries(run.tasks.map((t) => [t.taskKey, t.status])) as Record<string, TaskStatus>, [run.tasks]);
  const doneCount = run.tasks.filter((t) => !["pending", "running", "retrying"].includes(t.status)).length;

  async function cancel() {
    setCancelError(null);
    try {
      await api.cancelRun(v.api, run.id);
      reload();
    } catch (err) {
      setCancelError(errorMessage(err));
    }
  }

  const pipelineName = pipeline?.name ?? run.pipelineId.slice(0, 8);
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-1.5">
        <nav aria-label="Breadcrumb" className="text-sm text-slate-500 dark:text-slate-400">
          <Link href={v.href({ name: "pipelines" })} onNavigate={v.goPath} className="hover:text-slate-800 dark:hover:text-slate-200">
            ← Pipelines
          </Link>
          <span className="mx-1.5">/</span>
          <Link href={v.href({ name: "pipeline", id: run.pipelineId, tab: "runs" })} onNavigate={v.goPath} className="hover:text-slate-800 dark:hover:text-slate-200">
            {pipelineName}
          </Link>
          <span className="mx-1.5">/</span>
          <span className="text-slate-800 dark:text-slate-200">Run {run.id.slice(0, 8)}</span>
        </nav>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <h2 className="text-xl font-semibold text-slate-900 dark:text-slate-50">
              Run <span className="font-mono">{run.id.slice(0, 8)}</span>
            </h2>
            <StatusBadge status={run.status} />
          </div>
          {v.canWrite && active && (
            <Button onClick={cancel} disabled={run.cancelRequested}>
              {run.cancelRequested ? "Cancelling…" : "Cancel run"}
            </Button>
          )}
        </div>
      </div>

      {cancelError && <Banner tone="error">{cancelError}</Banner>}
      {run.error && <Banner tone={run.status === "canceled" ? "warn" : "error"}>{run.error}</Banner>}

      <StatStrip
        label="Run summary"
        stats={[
          { label: "Status", value: <StatusBadge status={run.status} />, mono: false },
          { label: "Started", value: formatStamp(runStart(run)), title: `${formatTime(runStart(run))} (${formatRelative(runStart(run))})` },
          { label: "Duration", value: run.startedAt ? formatDuration(run.startedAt, run.finishedAt, now) : "—" },
          {
            label: "Version",
            value: (
              <Link href={v.href({ name: "pipeline", id: run.pipelineId, version: run.pipelineVersion })} onNavigate={v.goPath} className={linkClass}>
                v{run.pipelineVersion}
              </Link>
            ),
          },
          { label: "Triggered by", value: run.trigger === "schedule" ? "Schedule" : run.triggeredBy, mono: false },
          { label: "Tasks", value: `${doneCount}/${run.tasks.length} done` },
        ]}
      />

      {graphError ? (
        <Banner tone="warn">The pipeline graph could not be loaded ({graphError}); the task table below is still accurate.</Banner>
      ) : (
        <DagCanvas label="Run DAG" refs={refs} resolved={resolved} selected={selected} onSelect={setSelected} statuses={statuses} theme={v.theme} height={RUN_CANVAS_HEIGHT} />
      )}

      <div className="relative overflow-x-auto">
        <table className={tableClass} aria-label="Tasks in this run">
          <thead className={theadClass}>
            <tr>
              <th className={thClass}>Node</th>
              <th className={thClass}>Task</th>
              <th className={thClass}>Status</th>
              <th className={thClass}>Attempts</th>
              <th className={thClass}>Started</th>
              <th className={thClass}>Duration</th>
              <th className={thClass}>Error</th>
              <th className={thClass}>
                <span className="sr-only">Logs</span>
              </th>
            </tr>
          </thead>
          <tbody className={tbodyClass}>
            {run.tasks.map((t) => (
              <tr key={t.taskKey} className={`${rowHoverClass} ${selected === t.taskKey ? "bg-indigo-50 dark:bg-indigo-950/40" : ""}`}>
                <td className={`${tdClass} font-mono text-xs`}>{t.taskKey}</td>
                <td className={tdClass}>{resolved[t.taskKey]?.task?.name ?? "—"}</td>
                <td className={tdClass}>
                  <StatusBadge status={t.status} />
                </td>
                <td className={`${tdClass} font-mono text-xs`}>{t.attempts}</td>
                <td className={`${tdClass} whitespace-nowrap font-mono text-xs`}>{t.startedAt ? formatStamp(t.startedAt) : "—"}</td>
                <td className={`${tdClass} whitespace-nowrap font-mono text-xs`}>{formatDuration(t.startedAt, t.finishedAt, now)}</td>
                <td className={`${tdClass} max-w-xs truncate text-xs text-red-700 dark:text-red-400`} title={t.error ?? undefined}>
                  {t.error ?? ""}
                </td>
                <td className={`${tdClass} text-right`}>
                  <Button size="sm" aria-pressed={selected === t.taskKey} onClick={() => setSelected(selected === t.taskKey ? null : t.taskKey)} aria-label={`Logs for ${t.taskKey}`}>
                    Logs
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <section aria-label="Logs" className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
            Logs — {selected ? <span className="font-mono">{selected}</span> : "whole run"}
          </h3>
          {selected && (
            <Button size="sm" onClick={() => setSelected(null)}>
              Show the whole run
            </Button>
          )}
        </div>
        <LogViewer api={v.api} runId={run.id} taskKey={selected} runActive={active} />
      </section>
    </div>
  );
}
