import type { ViewCtx } from "../context";
import { formatDuration } from "../format";
import type { RunDetail, TaskRef } from "../types";
import { Card, KVRows, Link, StatusBadge, linkClass, type KV } from "../ui/primitives";
import { effectiveParams } from "./ParamOverrides";
import { codeRefLabel, resolvedFromLabel, versionRefLabel, type ResolvedNode } from "./resolve";

/** Read-only facts about one DAG node — the Figma's side panel, rendered BELOW the canvas
 *  (docs/decisions/0016 §4 S2a). `lastRun` adds that run's result for this node when it has one. */
export function NodeDetails({ v, taskRef, node, lastRun, runCounts }: { v: ViewCtx; taskRef: TaskRef; node: ResolvedNode | undefined; lastRun: RunDetail | null; runCounts: boolean }) {
  const task = node?.task ?? null;
  const cfg = node?.config ?? null;

  const reference: KV[] = [
    { label: "Node key", value: taskRef.key, mono: true },
    {
      label: "Task",
      value: task ? (
        <Link href={v.href({ name: "task", id: task.id })} onNavigate={v.goPath} className={linkClass}>
          {task.name}
        </Link>
      ) : (
        <span className="text-red-600 dark:text-red-400">{node?.error ?? "loading…"}</span>
      ),
    },
    {
      label: "Version",
      value: taskRef.taskVersion === "latest" ? `Always follow latest → currently ${node ? resolvedFromLabel(node.from) : "…"}` : `Pinned to ${versionRefLabel(taskRef)}`,
    },
    { label: "Depends on", value: taskRef.dependsOn.length ? taskRef.dependsOn.join(", ") : "—", mono: true },
    {
      label: "Param overrides",
      value: Object.keys(taskRef.paramOverrides ?? {}).length ? <code className="whitespace-pre-wrap text-xs">{JSON.stringify(taskRef.paramOverrides, null, 2)}</code> : "— (runs with the task's own params)",
    },
  ];
  const config: KV[] = cfg
    ? [
        { label: "Code", value: codeRefLabel(cfg), mono: true },
        { label: "Runner", value: cfg.runner, mono: true },
        { label: "Retries", value: cfg.retry ? `${cfg.retry.maxRetries} · ${cfg.retry.backoff}, ${cfg.retry.delaySeconds}s` : "none", mono: true },
        { label: "Timeout", value: `${cfg.timeoutSeconds}s`, mono: true },
        { label: "Platform access", value: cfg.platformAccess ? "on" : "off" },
        {
          // What this node runs with: the task's params with this node's overrides merged over them.
          label: "Parameters",
          value: Object.keys(effectiveParams(cfg.params, taskRef.paramOverrides)).length ? (
            <code className="whitespace-pre-wrap text-xs">{JSON.stringify(effectiveParams(cfg.params, taskRef.paramOverrides), null, 2)}</code>
          ) : (
            "—"
          ),
        },
      ]
    : [];

  return (
    <Card
      label={`Task ${task?.name ?? taskRef.key} details`}
      title={
        <span className="flex items-center gap-2">
          <span>{task?.name ?? taskRef.key}</span>
          {task?.description && <span className="font-normal text-slate-500 dark:text-slate-400">— {task.description}</span>}
        </span>
      }
    >
      <LastRunStrip v={v} nodeKey={taskRef.key} lastRun={runCounts ? lastRun : null} />
      <div className="grid md:grid-cols-2 md:divide-x md:divide-slate-100 dark:md:divide-slate-800">
        <KVRows rows={reference} />
        {cfg ? <KVRows rows={config} /> : <p className="px-4 py-3 text-sm text-slate-500 dark:text-slate-400">{node?.from.kind === "none" && !node.error ? "This task has no saved configuration yet." : ""}</p>}
      </div>
    </Card>
  );
}

/** This node's result in the pipeline's latest run, with a link to its logs. Renders nothing when
 *  that run didn't include this node (or the node was re-pointed since — the caller decides). */
export function LastRunStrip({ v, nodeKey, lastRun }: { v: ViewCtx; nodeKey: string; lastRun: RunDetail | null }) {
  const tr = lastRun?.tasks.find((t) => t.taskKey === nodeKey);
  if (!lastRun || !tr) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-slate-200 px-4 py-2.5 text-sm dark:border-slate-700">
      <span className="text-slate-500 dark:text-slate-400">Last run</span>
      <StatusBadge status={tr.status} />
      <span className="font-mono text-xs">attempts {tr.attempts}</span>
      <span className="font-mono text-xs">{formatDuration(tr.startedAt, tr.finishedAt)}</span>
      {tr.error && (
        <span className="min-w-0 truncate text-xs text-red-600 dark:text-red-400" title={tr.error}>
          {tr.error}
        </span>
      )}
      <Link href={v.href({ name: "run", id: lastRun.id, task: nodeKey })} onNavigate={v.goPath} className={`${linkClass} ml-auto text-xs`}>
        Logs →
      </Link>
    </div>
  );
}
