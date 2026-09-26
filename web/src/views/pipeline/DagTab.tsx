import { useMemo, useState } from "react";
import type { ViewCtx } from "../../context";
import { DagCanvas } from "../../dag/DagCanvas";
import { NodeDetails } from "../../dag/NodeDetails";
import type { Resolved } from "../../dag/resolve";
import { formatRelative, formatStamp } from "../../format";
import { hasOverlappingPositions, autoLayout } from "../../graph";
import type { TaskRef } from "../../types";
import { Banner, Link, inputClass, linkClass } from "../../ui/primitives";
import type { LatestRun, PipelineData } from "./data";

// S2a — the DAG tab (docs/decisions/0016 §4). Phase 2 of the rebuild: the read side (status
// borders from the latest run, node details below the canvas). Editing arrives in phase 3.

export function DagTab({ v, d, refs: loadedRefs, resolved, latestRun }: { v: ViewCtx; d: PipelineData; refs: TaskRef[]; resolved: Resolved; latestRun: LatestRun }) {
  const p = d.pipeline;
  // A spec built straight against the API often has every node at {0,0}: lay those out once so the
  // canvas never looks empty or broken (they'd be drawn perfectly stacked otherwise).
  const refs = useMemo(() => (hasOverlappingPositions(loadedRefs) ? autoLayout(loadedRefs) : loadedRefs), [loadedRefs]);
  const [selected, setSelected] = useState<string | null>(null);
  const statuses = useMemo(() => latestRun.statusesFor(refs), [latestRun, refs]);
  const run = latestRun.run;
  const selectedRef = refs.find((r) => r.key === selected) ?? null;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <label className="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300">
          Showing
          <select
            aria-label="Pipeline version shown"
            className={`${inputClass} w-auto`}
            value={d.viewed ? String(d.viewed.version) : ""}
            onChange={(e) => v.go(e.target.value === "" ? { name: "pipeline", id: p.id } : { name: "pipeline", id: p.id, version: Number(e.target.value) })}
          >
            <option value="">{d.draftDiffers || !d.latest ? "Current draft" : `Current (v${d.latest.version})`}</option>
            {d.versions.map((ver) => (
              <option key={ver.version} value={ver.version}>
                v{ver.version} — {formatStamp(ver.createdAt)} — {ver.taskCount} task{ver.taskCount === 1 ? "" : "s"}
                {ver.notes ? ` — ${ver.notes}` : ""}
              </option>
            ))}
          </select>
        </label>
        {run && (
          <p className="text-xs text-slate-500 dark:text-slate-400">
            Status from{" "}
            <Link href={v.href({ name: "run", id: run.id })} onNavigate={v.goPath} className={`${linkClass} font-mono`}>
              run {run.id.slice(0, 8)}
            </Link>{" "}
            (v{run.pipelineVersion}, {formatRelative(run.startedAt ?? run.createdAt)})
          </p>
        )}
      </div>

      {d.viewed && d.latest && d.viewed.version !== d.latest.version && (
        <Banner tone="info">
          Viewing v{d.viewed.version} of {d.latest.version} (saved {formatStamp(d.viewed.createdAt)}). Nothing pinned to a version changes when you look at or save from an old one.
        </Banner>
      )}

      <DagCanvas
        label="Pipeline DAG"
        refs={refs}
        resolved={resolved}
        selected={selected}
        onSelect={setSelected}
        statuses={statuses}
        theme={v.theme}
        empty={
          <div className="pointer-events-auto max-w-sm rounded-lg border border-dashed border-slate-300 bg-white p-6 text-center dark:border-slate-600 dark:bg-slate-900">
            <p className="text-sm font-medium text-slate-800 dark:text-slate-100">This pipeline has no tasks yet</p>
          </div>
        }
      />

      {selectedRef ? (
        <NodeDetails v={v} taskRef={selectedRef} node={resolved[selectedRef.key]} lastRun={run} runCounts={selectedRef.key in statuses} />
      ) : (
        refs.length > 0 && <p className="text-sm text-slate-500 dark:text-slate-400">Select a task to see its details here.</p>
      )}
    </div>
  );
}
