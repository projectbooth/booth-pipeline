import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, type ApiContext } from "../../api/client";
import { errorMessage, useInterval } from "../../hooks";
import { isActive } from "../../runs";
import type { Pipeline, PipelineDraft, PipelineVersion, PipelineVersionSummary, RunDetail, TaskRef, TaskStatus } from "../../types";

// Everything the pipeline detail page (S2) shows that isn't specific to one tab.

export interface PipelineData {
  pipeline: Pipeline;
  versions: PipelineVersionSummary[]; // newest first
  /** The latest immutable version (null: never saved as a version). */
  latest: PipelineVersion | null;
  /** The explicitly requested old version (`/v/:n`), when there is one. */
  viewed: PipelineVersion | null;
  draft: PipelineDraft | null;
  /** True only when the draft's spec is actually different from the latest version's — a
   *  version-save also rewrites the draft to match, so "a draft row exists" alone means nothing. */
  draftDiffers: boolean;
}

export async function loadPipeline(ctx: ApiContext, id: string, version?: number): Promise<PipelineData> {
  const [pipeline, versions] = await Promise.all([api.getPipeline(ctx, id), api.listVersions(ctx, id)]);
  const [latest, draft] = await Promise.all([
    pipeline.latestVersion > 0 ? api.getVersion(ctx, id, "latest") : Promise.resolve(null),
    pipeline.hasDraft ? api.getPipelineDraft(ctx, id).catch((e: unknown) => (e instanceof ApiError && e.status === 404 ? null : Promise.reject(e))) : Promise.resolve(null),
  ]);
  const viewed = version === undefined ? null : version === latest?.version ? latest : await api.getVersion(ctx, id, version);
  const draftDiffers = draft !== null && JSON.stringify(draft.spec.tasks) !== JSON.stringify(latest?.spec.tasks ?? []);
  return { pipeline, versions: versions.items, latest, viewed, draft, draftDiffers };
}

/** The spec a pipeline's canvas starts from: an explicitly viewed version, else the draft (ADR
 *  0073 — always at least as fresh as the latest version), else the latest version, else empty. */
export function currentSpec(d: PipelineData): TaskRef[] {
  return (d.viewed ?? d.draft ?? d.latest)?.spec.tasks ?? [];
}

/** The version `Run now` / the scheduler would run right now: the pin, else the latest saved
 *  version (service.start_run — NEVER the draft). null: nothing saved yet, so nothing can run. */
export function runnableVersion(p: Pipeline): { version: number; pinned: boolean } | null {
  if (p.pinnedVersion) return { version: p.pinnedVersion, pinned: true };
  return p.latestVersion > 0 ? { version: p.latestVersion, pinned: false } : null;
}

export interface LatestRun {
  run: RunDetail | null;
  /** Node statuses from that run, keyed by node key, counting only nodes that still reference the
   *  same task the run executed (a re-pointed node's old status says nothing about it). */
  statusesFor: (refs: TaskRef[]) => Record<string, TaskStatus>;
  error: string | null;
  reload: () => void;
}

/** The pipeline's most recent run, polled every 2 s while it's active and not at all otherwise. */
export function useLatestRun(ctx: ApiContext, pipelineId: string, pollMs = 2000): LatestRun {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [runRefs, setRunRefs] = useState<Map<string, string>>(new Map()); // node key -> task id, as that run's version had it
  const [error, setError] = useState<string | null>(null);
  const specCache = useRef(new Map<number, Map<string, string>>());
  const generation = useRef(0);

  const reload = useCallback(async () => {
    const mine = ++generation.current;
    try {
      const page = await api.listRuns(ctx, { pipelineId, limit: 1 });
      const summary = page.items[0];
      if (!summary) {
        if (mine === generation.current) setRun(null);
        return;
      }
      const detail = await api.getRun(ctx, summary.id);
      let refs = specCache.current.get(detail.pipelineVersion);
      if (!refs) {
        const v = await api.getVersion(ctx, pipelineId, detail.pipelineVersion);
        refs = new Map(v.spec.tasks.map((t) => [t.key, t.taskId]));
        specCache.current.set(detail.pipelineVersion, refs);
      }
      if (mine !== generation.current) return;
      setRun(detail);
      setRunRefs(refs);
      setError(null);
    } catch (err) {
      if (mine === generation.current) setError(errorMessage(err));
    }
  }, [ctx, pipelineId]);

  useEffect(() => {
    void reload();
  }, [reload]);
  useInterval(() => void reload(), pollMs, isActive(run));

  const statusesFor = useCallback(
    (refs: TaskRef[]) => {
      const out: Record<string, TaskStatus> = {};
      if (!run) return out;
      const byKey = new Map(run.tasks.map((t) => [t.taskKey, t.status]));
      for (const r of refs) {
        const s = byKey.get(r.key);
        if (s && runRefs.get(r.key) === r.taskId) out[r.key] = s;
      }
      return out;
    },
    [run, runRefs],
  );

  return { run, statusesFor, error, reload: () => void reload() };
}

/** Downloads one pipeline version as YAML (ADR 0071 phase 4's export). */
export async function downloadYaml(ctx: ApiContext, p: Pipeline, version: number): Promise<void> {
  const text = await api.exportVersion(ctx, p.id, version);
  const url = URL.createObjectURL(new Blob([text], { type: "application/yaml" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `${p.name.replace(/[^\w.-]+/g, "_")}-v${version}.yaml`;
  a.click();
  URL.revokeObjectURL(url);
}
