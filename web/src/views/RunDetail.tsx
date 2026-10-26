import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { ViewCtx } from "../context";
import { DagCanvas } from "../dag/DagCanvas";
import { useExpandedCanvas } from "../dag/useExpandedCanvas";
import { useResolved } from "../dag/resolve";
import { formatDuration, formatRelative, formatStamp, formatTime } from "../format";
import { autoLayout, hasOverlappingPositions } from "../graph";
import { errorMessage, useInterval, useLoad } from "../hooks";
import { isActive, runStart } from "../runs";
import type { Pipeline, PipelineVersion, RunDetail as Run, TaskRef, TaskStatus } from "../types";
import {
  Badge,
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
import { ownerLabel } from "./pipeline/RunsAs";
import { LogViewer } from "./run/LogViewer";
import { TaskOutputPanel } from "./run/TaskOutput";

// S3 — one Run (docs/decisions/0016 §4): its DAG coloured by each node's status, a per-node
// table, and logs for the whole run or one node. Refreshes while active, settles once it ends.

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
  // Compact by default, as on the pipeline page; remembered separately, per browser.
  const [canvasExpanded, toggleCanvas] = useExpandedCanvas("booth-pipeline.run-dag-expanded");
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
  const selectedTask = run.tasks.find((t) => t.taskKey === selected);
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
      {run.error && <Banner tone={run.status === "canceled" || run.status === "skipped" ? "warn" : "error"}>{run.status === "skipped" ? `Skipped: ${run.error}` : run.error}</Banner>}

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
          { label: "Triggered by", value: run.trigger === "schedule" ? "Schedule" : run.trigger === "event" ? `Event — ${run.triggeredBy}` : run.triggeredBy, mono: false },
          ...(run.scheduledFor
            ? [{ label: "Scheduled for", value: formatStamp(run.scheduledFor), title: `${formatTime(run.scheduledFor)}: the slot this run is for, however late it started` }]
            : []),
          ...(run.actsAs ? [{ label: "Ran as", value: ownerLabel(run.actsAs), mono: false }] : []),
          { label: "Tasks", value: `${doneCount}/${run.tasks.length} done` },
        ]}
      />

      {run.triggerEvents?.events && run.triggerEvents.events.length > 0 && (
        <details className="rounded-lg border border-slate-200 p-3 text-sm dark:border-slate-700">
          <summary className="cursor-pointer text-slate-700 dark:text-slate-200">
            What fired this run ({run.triggerEvents.count} event{run.triggerEvents.count === 1 ? "" : "s"}
            {run.triggerEvents.count > run.triggerEvents.events.length ? `, the first ${run.triggerEvents.events.length} shown` : ""})
          </summary>
          <ul aria-label="Trigger events" className="mt-2 flex flex-col gap-0.5 font-mono text-xs text-slate-600 dark:text-slate-300">
            {run.triggerEvents.events.map((e, i) => (
              <li key={i}>{e.path !== undefined ? `${e.backendId}:${e.path}` : `${e.operation ?? "write"} ${e.apiSlug}${e.table ? ` (${e.table})` : ""}`}</li>
            ))}
          </ul>
        </details>
      )}
      {run.triggerEvents?.missedEvents && <Banner tone="warn">Fired because the event bus may have lost events; tasks should scan their inbox.</Banner>}

      {graphError ? (
        <Banner tone="warn">The pipeline graph could not be loaded ({graphError}); the task table below is still accurate.</Banner>
      ) : (
        <div className="flex flex-col gap-1.5">
          <div className="flex justify-end">
            <Button size="sm" variant="ghost" aria-pressed={canvasExpanded} onClick={toggleCanvas}>
              {canvasExpanded ? "Shrink canvas" : "Expand canvas"}
            </Button>
          </div>
          <DagCanvas
            label="Run DAG"
            refs={refs}
            resolved={resolved}
            selected={selected}
            onSelect={setSelected}
            statuses={statuses}
            theme={v.theme}
            size={canvasExpanded ? "run" : "runCompact"}
          />
        </div>
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
                  <div className="flex flex-wrap items-center gap-1.5">
                    <StatusBadge status={t.status} />
                    {t.external?.kind === "spark" && (
                      <a
                        href={`/spark/runs/${encodeURIComponent(t.external.id)}`}
                        className="rounded-full border border-amber-300 px-2 py-0.5 text-xs text-amber-900 hover:underline dark:border-amber-700 dark:text-amber-200"
                        title={t.external.reason ?? `booth-spark application ${t.external.id}`}
                      >
                        Spark · {t.external.state} · {t.external.id.slice(0, 8)}
                      </a>
                    )}
                    {t.secrets && t.secrets.length > 0 && (
                      <Badge tone="slate" title="Task secrets this attempt was given (ADR 0128); their values are never shown">
                        Secrets: {t.secrets.join(", ")}
                      </Badge>
                    )}
                  </div>
                </td>
                <td className={`${tdClass} font-mono text-xs`}>{t.attempts}</td>
                <td className={`${tdClass} whitespace-nowrap font-mono text-xs`}>{t.startedAt ? formatStamp(t.startedAt) : "—"}</td>
                <td className={`${tdClass} whitespace-nowrap font-mono text-xs`}>{formatDuration(t.startedAt, t.finishedAt, now)}</td>
                <td
                  className={`${tdClass} max-w-xs truncate text-xs ${t.status === "skipped" ? "text-amber-800 dark:text-amber-300" : "text-red-700 dark:text-red-400"}`}
                  title={t.error ?? undefined}
                >
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

      {v.canWrite && selectedTask && <TaskOutputPanel task={selectedTask} />}

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
